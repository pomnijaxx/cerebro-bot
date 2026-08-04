#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CEREBRO-BOT — Bot de Telegram com cérebro OpenCode
====================================================
O bot recebe mensagens do Telegram, manda pro OpenCode (subprocesso),
captura o que ele responde e devolve pro chat. Suporta múltiplos agentes
(personas) e roda em "turnos" dentro do GitHub Actions (máx 5h30 por job,
porque o limite hard do GH Actions é 6h por job).

Envs:
  TELEGRAM_BOT_TOKEN   token do BotFather (obrigatório)
  ADMIN_ID             id numérico do dono (recebe avisos de shift)
  ALLOWED_IDS          lista de ids permitidos separados por vírgula (vazio = qualquer um)
  DEFAULT_AGENT        agente padrão para chats novos (helper)
  DEFAULT_MODEL        modelo opencode (ex: openrouter/deepseek/deepseek-chat-v3-0324:free)
  AUTO_APPROVE         1 = --dangerously-skip-permissions (default 1)
  BRIDGE_TIMEOUT       timeout por mensagem em segundos (default 900)
  SHIFT_NAME           nome do turno (turno-a / turno-b) — usado nos avisos
  MEMORY_PUSH_MINUTES  intervalo de snapshot da memória pro git (default 15, 0 = só no fim)
  HEARTBEAT_MINUTES    se > 0, manda batimento pro admin a cada N min
  TITLE_PREFIX         prefixo do título da sessão opencode (default "tg")
"""

import asyncio
import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ----------------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------------

# Carrega .env local (se existir) antes de ler as env vars.
BASE_DIR = Path(__file__).resolve().parent.parent
from dotenv import load_dotenv  # noqa: E402

load_dotenv(BASE_DIR / ".env")

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0") or 0)
ALLOWED_IDS = {
    int(x.strip()) for x in os.environ.get("ALLOWED_IDS", "").split(",") if x.strip()
}
DEFAULT_AGENT = os.environ.get("DEFAULT_AGENT", "helper")
DEFAULT_MODEL = os.environ.get("DEFAULT_MODEL", "").strip()
AUTO_APPROVE = os.environ.get("AUTO_APPROVE", "1") == "1"
BRIDGE_TIMEOUT = int(os.environ.get("BRIDGE_TIMEOUT", "900"))
SHIFT_NAME = os.environ.get("SHIFT_NAME", "shift")
MEMORY_PUSH_MINUTES = int(os.environ.get("MEMORY_PUSH_MINUTES", "15"))
HEARTBEAT_MINUTES = int(os.environ.get("HEARTBEAT_MINUTES", "0"))
TITLE_PREFIX = os.environ.get("TITLE_PREFIX", "tg")

LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
MEMORY_FILE = BASE_DIR / "memory.json"
BRANCH = os.environ.get("GITHUB_REF_NAME", "main")

AGENTS = json.loads((BASE_DIR / "bot" / "agents.json").read_text(encoding="utf-8"))

MAX_HISTORY = 40        # msgs guardadas por chat na memória persistente
SEED_MESSAGES = 12      # msgs injetadas como contexto no 1º turno do shift
MAX_REPLY = 4000        # limite do Telegram por mensagem
COOLDOWN_SEC = 2        # anti-spam por usuário
UPTIME_START = time.time()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "shift.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("cerebro")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ----------------------------------------------------------------------------
# MEMÓRIA PERSISTENTE (sobrevive entre turnos via git)
# ----------------------------------------------------------------------------

def load_memory() -> dict:
    if MEMORY_FILE.exists():
        try:
            return json.loads(MEMORY_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            log.warning("memoria corrompida, recriando: %s", e)
    return {"chats": {}}


def save_memory(mem: dict) -> None:
    MEMORY_FILE.write_text(
        json.dumps(mem, ensure_ascii=False, indent=1), encoding="utf-8"
    )


def git_push(retries: int = 3) -> None:
    """Commit + push da memória pro repo (GITHUB_TOKEN é injetado pelo Actions)."""
    cmds = [
        ["git", "config", "user.name", "cerebro-bot"],
        ["git", "config", "user.email", "cerebro-bot@users.noreply.github.com"],
        ["git", "add", "memory.json"],
        ["git", "commit", "-m", f"chore: snapshot memoria {now_iso()} [skip ci]"],
        ["git", "pull", "--rebase", "origin", BRANCH],
        ["git", "push", "origin", BRANCH],
    ]
    for attempt in range(retries):
        ok = True
        for cmd in cmds:
            r = subprocess.run(cmd, capture_output=True, text=True, cwd=BASE_DIR)
            if r.returncode != 0 and "nothing to commit" not in r.stdout + r.stderr:
                log.info("git %s -> %s", cmd[0], (r.stderr or r.stdout).strip()[:200])
                if cmd[0] == "git" and cmd[1] == "commit":
                    ok = True  # "nothing to commit" é esperado às vezes
                else:
                    ok = False
                    break
        if ok:
            return
        time.sleep(5)
    log.warning("git push falhou após %s tentativas", retries)


# ----------------------------------------------------------------------------
# PONTE OPencode (subprocesso + parser NDJSON)
# ----------------------------------------------------------------------------

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def parse_stream(stdout: str):
    """Extrai (resposta, session_id, erro) do NDJSON emitido por `opencode run --format json`."""
    texts: list[str] = []
    seen: set[str] = set()
    session_id = None
    error = None
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        sid = ev.get("sessionID")
        if sid:
            session_id = sid
        t = ev.get("type")
        if t == "text":
            part = ev.get("part") or {}
            if part.get("type") == "text":
                pid = part.get("id")
                if pid in seen:
                    continue
                seen.add(pid)
                txt = ANSI_RE.sub("", part.get("text", "")).strip()
                if txt:
                    texts.append(txt)
        elif t == "error":
            err = ev.get("error") or {}
            if isinstance(err, dict):
                error = err.get("message") or json.dumps(err)
            else:
                error = str(err)
    return "\n".join(texts).strip(), session_id, error


def run_bridge(
    prompt: str,
    agent: str,
    model: str,
    session_id: str | None,
    chat_id: int,
) -> tuple[str, str | None, str | None]:
    """Executa uma rodada do OpenCode e devolve (resposta, nova_session, erro)."""
    cmd = ["opencode", "run", "--format", "json", "--print-logs"]
    if agent:
        cmd += ["--agent", agent]
    if model:
        cmd += ["--model", model]
    if session_id:
        cmd += ["--session", session_id]
    cmd += ["--title", f"{TITLE_PREFIX}:{chat_id}"]
    if AUTO_APPROVE:
        cmd += ["--dangerously-skip-permissions"]
    cmd.append(prompt)

    log.info("bridge | chat=%s agent=%s session=%s len=%s", chat_id, agent, session_id, len(prompt))
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=BRIDGE_TIMEOUT,
            cwd=BASE_DIR,
        )
    except subprocess.TimeoutExpired:
        return "", session_id, f"tempo esgotado ({BRIDGE_TIMEOUT}s) — o cérebro travou, manda de novo"
    except FileNotFoundError:
        return "", session_id, "opencode não está instalado neste ambiente"

    answer, new_session, err = parse_stream(proc.stdout)
    if not answer and not err:
        stderr_tail = ANSI_RE.sub("", proc.stderr or "").strip().splitlines()[-3:]
        err = "\n".join(stderr_tail) or f"sem resposta (exit {proc.returncode})"
    return answer or "", new_session or session_id, err


# ----------------------------------------------------------------------------
# ESTADO POR CHAT
# ----------------------------------------------------------------------------

MEMORY = load_memory()
CHATS: dict[int, dict] = {}
LOCK = asyncio.Lock()          # serializa o bridge (1 OpenCode por vez)
COOLDOWNS: dict[int, float] = {}


def chat_state(chat_id: int) -> dict:
    if chat_id not in CHATS:
        raw = MEMORY.get("chats", {}).get(str(chat_id), {})
        CHATS[chat_id] = {
            "agent": raw.get("agent") or DEFAULT_AGENT,
            "model": raw.get("model") or DEFAULT_MODEL,
            "session_id": None,          # sessão opencode vale só dentro do turno
            "history": deque(raw.get("history", [])[-MAX_HISTORY:], maxlen=MAX_HISTORY),
        }
    return CHATS[chat_id]


def persist_chats() -> None:
    chats = {}
    for cid, st in CHATS.items():
        chats[str(cid)] = {
            "agent": st["agent"],
            "model": st["model"],
            "history": list(st["history"])[-MAX_HISTORY:],
        }
    save_memory({"chats": chats})


def seed_prompt(st: dict, user_text: str) -> str:
    """Injeta o histórico do turno anterior como contexto no 1º turno deste shift."""
    hist = [h for h in st["history"] if h.get("text")]
    if not hist:
        return user_text
    lines = ["[Contexto de conversas anteriores (use como memória):]"]
    for h in hist[-SEED_MESSAGES:]:
        lines.append(f"{h['role']}: {h['text'][:1500]}")
    lines.append("[Fim do contexto]")
    lines.append("")
    lines.append(user_text)
    return "\n".join(lines)


def split_reply(text: str) -> list[str]:
    if len(text) <= MAX_REPLY:
        return [text]
    chunks, cur = [], ""
    for para in text.split("\n\n"):
        if len(cur) + len(para) + 2 > MAX_REPLY:
            chunks.append(cur)
            cur = para
        else:
            cur = f"{cur}\n\n{para}" if cur else para
    if cur:
        chunks.append(cur)
    return chunks


# ----------------------------------------------------------------------------
# HANDLERS
# ----------------------------------------------------------------------------

def is_allowed(user_id: int) -> bool:
    return not ALLOWED_IDS or user_id in ALLOWED_IDS


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    names = ", ".join(f"/agent {k}" for k in AGENTS)
    await ctx.bot.send_message(
        chat_id,
        "🧠 Cérebro online.\n\n"
        "Manda qualquer mensagem que eu respondo via OpenCode.\n\n"
        "Comandos:\n"
        f"/agent <nome> — trocar persona ({names})\n"
        "/agents — listar personas\n"
        "/model <provider/modelo> — trocar modelo\n"
        "/reset — limpar minha memória deste chat\n"
        "/status — status do cérebro",
    )


async def cmd_agents(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    lines = [f"🧠 {len(AGENTS)} agentes:\n"]
    for name, info in AGENTS.items():
        lines.append(f"• <b>{name}</b> — {info['desc']}")
    await ctx.bot.send_message(update.effective_chat.id, "\n".join(lines), parse_mode="HTML")


async def cmd_agent(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    args = ctx.args
    if not args:
        await cmd_agents(update, ctx)
        return
    name = args[0].lower()
    if name not in AGENTS:
        await ctx.bot.send_message(
            chat_id, f"Agente '{name}' não existe. Tenta: {', '.join(AGENTS)}"
        )
        return
    st = chat_state(chat_id)
    st["agent"] = name
    st["session_id"] = None  # sessão nova para o agente novo
    persist_chats()
    await ctx.bot.send_message(chat_id, f"🤖 Persona trocada para <b>{name}</b>: {AGENTS[name]['desc']}", parse_mode="HTML")


async def cmd_model(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    model = " ".join(ctx.args).strip()
    if not model:
        await ctx.bot.send_message(chat_id, f"Modelo atual: <code>{chat_state(chat_id)['model'] or 'padrão do OpenCode'}</code>\nUse: /model provider/modelo", parse_mode="HTML")
        return
    chat_state(chat_id)["model"] = model
    chat_state(chat_id)["session_id"] = None
    persist_chats()
    await ctx.bot.send_message(chat_id, f"🧬 Modelo trocado para <code>{model}</code>", parse_mode="HTML")


async def cmd_reset(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    st = chat_state(chat_id)
    st["history"].clear()
    st["session_id"] = None
    persist_chats()
    await ctx.bot.send_message(chat_id, "🧹 Memória deste chat zerada. Recomeçando do zero.")


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    st = chat_state(chat_id)
    up = int(time.time() - UPTIME_START)
    hh, mm, ss = up // 3600, (up % 3600) // 60, up % 60
    await ctx.bot.send_message(
        chat_id,
        f"⚙️ <b>Status</b>\n"
        f"Shift: <code>{SHIFT_NAME}</code>\n"
        f"Uptime: {hh}h{mm:02d}m{ss:02d}s\n"
        f"Agente: <code>{st['agent']}</code>\n"
        f"Modelo: <code>{st['model'] or 'padrão'}</code>\n"
        f"Memória do chat: {len(st['history'])} msgs\n"
        f"Sessão opencode: {'ativa' if st['session_id'] else 'nova'}",
        parse_mode="HTML",
    )


async def cmd_ping(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await ctx.bot.send_message(update.effective_chat.id, "pong 🫀")


async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id if update.effective_user else 0
    text = (update.effective_message.text or "").strip()

    if not is_allowed(user_id):
        await ctx.bot.send_message(chat_id, "⛔ Sem acesso.")
        return
    if not text:
        return

    now = time.time()
    last = COOLDOWNS.get(user_id, 0)
    if now - last < COOLDOWN_SEC:
        return
    COOLDOWNS[user_id] = now

    st = chat_state(chat_id)
    prompt = text if st["session_id"] else seed_prompt(st, text)

    async def typing_loop():
        while True:
            await ctx.bot.send_chat_action(chat_id, ChatAction.TYPING)
            await asyncio.sleep(12)

    t_task = asyncio.create_task(typing_loop())
    try:
        async with LOCK:
            answer, new_session, err = await asyncio.to_thread(
                run_bridge, prompt, st["agent"], st["model"], st["session_id"], chat_id
            )
    finally:
        t_task.cancel()

    if err and not answer:
        await ctx.bot.send_message(chat_id, f"💥 <b>Erro no cérebro:</b>\n<code>{err[:1500]}</code>", parse_mode="HTML")
        return

    if new_session:
        st["session_id"] = new_session
    st["history"].append({"role": "user", "text": text})
    st["history"].append({"role": "assistant", "text": answer})
    persist_chats()

    for chunk in split_reply(answer):
        await ctx.bot.send_message(chat_id, chunk)


# ----------------------------------------------------------------------------
# SHIFT LIFECYCLE
# ----------------------------------------------------------------------------

async def notify(text: str) -> None:
    if not ADMIN_ID:
        return
    try:
        app = Application.builder().token(TOKEN).build()
        await app.bot.send_message(ADMIN_ID, text)
        await app.shutdown()
    except Exception as e:
        log.warning("falha ao notificar admin: %s", e)


async def post_init(app: Application) -> None:
    await app.bot.set_my_commands(
        [
            ("start", "Iniciar"),
            ("agent", "Trocar persona"),
            ("agents", "Listar personas"),
            ("model", "Trocar modelo"),
            ("reset", "Limpar memória"),
            ("status", "Status"),
            ("ping", "Ping"),
        ]
    )
    # Loops de fundo: snapshot da memória e batimento pro admin
    if MEMORY_PUSH_MINUTES > 0:
        asyncio.get_running_loop().create_task(memory_loop())
    if HEARTBEAT_MINUTES > 0 and ADMIN_ID:
        asyncio.get_running_loop().create_task(heartbeat_loop())
    model_desc = DEFAULT_MODEL or "padrão do OpenCode"
    await notify(
        f"🟢 <b>Shift <code>{SHIFT_NAME}</code> online</b>\n"
        f"Agente padrão: <code>{DEFAULT_AGENT}</code>\n"
        f"Modelo: <code>{model_desc}</code>",
    )
    log.info("shift %s online | modelo=%s", SHIFT_NAME, model_desc)


async def memory_loop() -> None:
    if MEMORY_PUSH_MINUTES <= 0:
        return
    while True:
        await asyncio.sleep(MEMORY_PUSH_MINUTES * 60)
        persist_chats()
        await asyncio.to_thread(git_push)
        log.info("memória enviada ao repo")


async def heartbeat_loop() -> None:
    if HEARTBEAT_MINUTES <= 0 or not ADMIN_ID:
        return
    while True:
        await asyncio.sleep(HEARTBEAT_MINUTES * 60)
        await notify(f"🫀 shift <code>{SHIFT_NAME}</code> vivo", )


def final_flush() -> None:
    persist_chats()
    git_push()
    log.info("flush final: memória salva e enviada")


def main() -> None:
    if not TOKEN:
        log.error("TELEGRAM_BOT_TOKEN não definido")
        sys.exit(1)

    app = Application.builder().token(TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("agents", cmd_agents))
    app.add_handler(CommandHandler("agent", cmd_agent))
    app.add_handler(CommandHandler("model", cmd_model))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    log.info("iniciando polling...")
    # Roda até receber SIGTERM (timeout do GH Actions) ou SIGINT.
    app.run_polling(stop_signals=[signal.SIGTERM, signal.SIGINT])

    # Turno acabou: salva memória, push pro git, avisa admin.
    try:
        final_flush()
    except Exception as e:
        log.error("erro no flush final: %s", e)
    asyncio.run(notify(f"🔴 <b>Shift <code>{SHIFT_NAME}</code> encerrado.</b>\nVolto no próximo turno."))


if __name__ == "__main__":
    main()
