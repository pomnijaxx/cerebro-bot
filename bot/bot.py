#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CEREBRO-BOT v2 — Bot de Telegram com cérebro OpenCode
=======================================================
Bridging: mensagens do Telegram -> OpenCode (subprocesso) -> resposta de volta.
Roda em turnos de 5h30 dentro do GitHub Actions (limite hard de 6h por job).

v2 upgrades:
  - VERIFICAÇÃO ANTI-BOT: usuário novo recebe código aleatório e precisa
    responder com ele pra liberar o cérebro (3 tentativas, expira em 120s).
    Whitelist entra direto. Verificados ficam salvos em memory.json.
  - /broadcast do admin
  - /stats com tokens e custo (do evento step_finish do opencode)
  - botões inline no /start pra trocar de agente
  - contexto de reply (usuário responde a uma msg do bot -> ela vira contexto)
  - log das conversas em arquivos por chat (logs/conversas/<chat_id>.md)
  - fila visível: se o cérebro tá ocupado, avisa quantas msgs esperando
  - 1 retry em falha transitória do opencode
  - suporte a grupo: só responde se for mencionado ou reply em msg do bot
"""

import asyncio
import json
import logging
import os
import random
import re
import secrets
import signal
import subprocess
import sys
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ChatType
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ----------------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent.parent

try:
    from dotenv import load_dotenv  # noqa: E402

    load_dotenv(BASE_DIR / ".env")
except ImportError:
    pass

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
VERIFY_REQUIRED = os.environ.get("VERIFY_REQUIRED", "1") == "1"
VERIFY_TTL = int(os.environ.get("VERIFY_TTL", "120"))
VERIFY_MAX_TRIES = int(os.environ.get("VERIFY_MAX_TRIES", "3"))

LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
CONV_DIR = LOG_DIR / "conversas"
CONV_DIR.mkdir(parents=True, exist_ok=True)
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
# MEMÓRIA PERSISTENTE
# ----------------------------------------------------------------------------

def load_memory() -> dict:
    if MEMORY_FILE.exists():
        try:
            return json.loads(MEMORY_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            log.warning("memoria corrompida, recriando: %s", e)
    return {"chats": {}, "verified": []}


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
                    ok = True
                else:
                    ok = False
                    break
        if ok:
            return
        time.sleep(5)
    log.warning("git push falhou após %s tentativas", retries)


# ----------------------------------------------------------------------------
# PONTE OPencode
# ----------------------------------------------------------------------------

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def parse_stream(stdout: str):
    """Extrai (resposta, session_id, erro, stats) do NDJSON do `opencode run --format json`."""
    texts: list[str] = []
    seen: set[str] = set()
    session_id = None
    error = None
    stats = {"tokens": 0, "cost": 0.0, "input": 0, "output": 0, "reasoning": 0}
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
        elif t == "step_finish":
            part = ev.get("part") or {}
            tk = part.get("tokens") or {}
            stats["tokens"] += int(tk.get("total", 0) or 0)
            stats["input"] += int(tk.get("input", 0) or 0)
            stats["output"] += int(tk.get("output", 0) or 0)
            stats["reasoning"] += int(tk.get("reasoning", 0) or 0)
            stats["cost"] += float(part.get("cost", 0) or 0)
        elif t == "error":
            err = ev.get("error") or {}
            if isinstance(err, dict):
                error = err.get("message") or json.dumps(err)
            else:
                error = str(err)
    return "\n".join(texts).strip(), session_id, error, stats


def run_bridge(
    prompt: str,
    agent: str,
    model: str,
    session_id: str | None,
    chat_id: int,
) -> tuple[str, str | None, str | None, dict]:
    """Uma rodada do OpenCode. Retorna (resposta, nova_session, erro, stats)."""
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
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=BRIDGE_TIMEOUT, cwd=BASE_DIR)
    except subprocess.TimeoutExpired:
        return "", session_id, f"tempo esgotado ({BRIDGE_TIMEOUT}s) — o cérebro travou, manda de novo", {}
    except FileNotFoundError:
        return "", session_id, "opencode não está instalado neste ambiente", {}

    answer, new_session, err, stats = parse_stream(proc.stdout)
    if not answer and not err:
        stderr_tail = ANSI_RE.sub("", proc.stderr or "").strip().splitlines()[-3:]
        err = "\n".join(stderr_tail) or f"sem resposta (exit {proc.returncode})"
    return answer or "", new_session or session_id, err, stats


# ----------------------------------------------------------------------------
# ESTADO
# ----------------------------------------------------------------------------

MEMORY = load_memory()
CHATS: dict[int, dict] = {}
LOCK = asyncio.Lock()
COOLDOWNS: dict[int, float] = {}
VERIFY: dict[int, dict] = {}        # user_id -> {code, tries, expires, message}
BLOCKED: dict[int, float] = {}      # user_id -> timestamp de bloqueio
BOT_USERNAME: str = ""
QUEUE_POS: int = 0


def chat_state(chat_id: int) -> dict:
    if chat_id not in CHATS:
        raw = MEMORY.get("chats", {}).get(str(chat_id), {})
        CHATS[chat_id] = {
            "agent": raw.get("agent") or DEFAULT_AGENT,
            "model": raw.get("model") or DEFAULT_MODEL,
            "session_id": None,
            "history": deque(raw.get("history", [])[-MAX_HISTORY:], maxlen=MAX_HISTORY),
            "tokens": int(raw.get("tokens", 0) or 0),
            "cost": float(raw.get("cost", 0) or 0),
        }
    return CHATS[chat_id]


def persist_chats() -> None:
    chats = {}
    for cid, st in CHATS.items():
        chats[str(cid)] = {
            "agent": st["agent"],
            "model": st["model"],
            "history": list(st["history"])[-MAX_HISTORY:],
            "tokens": st["tokens"],
            "cost": st["cost"],
        }
    save_memory({"chats": chats, "verified": list(MEMORY.get("verified", []))})


def seed_prompt(st: dict, user_text: str) -> str:
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


def log_conversation(chat_id: int, role: str, text: str) -> None:
    try:
        f = CONV_DIR / f"{chat_id}.md"
        with f.open("a", encoding="utf-8") as fh:
            fh.write(f"\n## {now_iso()} [{role}]\n{text}\n")
    except Exception as e:
        log.warning("erro no log de conversa: %s", e)


# ----------------------------------------------------------------------------
# VERIFICAÇÃO ANTI-BOT
# ----------------------------------------------------------------------------

def is_verified(user_id: int) -> bool:
    if user_id in ALLOWED_IDS:
        return True
    return user_id in MEMORY.get("verified", [])


def mark_verified(user_id: int) -> None:
    if user_id not in ALLOWED_IDS:
        verified = MEMORY.setdefault("verified", [])
        if user_id not in verified:
            verified.append(user_id)
    persist_chats()


def start_verify(user_id: int, original_message: str) -> str:
    code = str(secrets.randbelow(10000)).zfill(4)
    VERIFY[user_id] = {
        "code": code,
        "tries": 0,
        "expires": time.time() + VERIFY_TTL,
        "message": original_message,
    }
    return code


def try_verify(user_id: int, text: str) -> str | None:
    """Tenta validar o código. Retorna None se ok, senão mensagem de erro."""
    pend = VERIFY.get(user_id)
    if not pend:
        return None
    if time.time() > pend["expires"]:
        VERIFY.pop(user_id, None)
        return "⏳ Código expirado. Manda qualquer mensagem pra ganhar um novo."
    if text == pend["code"]:
        mark_verified(user_id)
        VERIFY.pop(user_id, None)
        return None
    pend["tries"] += 1
    if pend["tries"] >= VERIFY_MAX_TRIES:
        VERIFY.pop(user_id, None)
        BLOCKED[user_id] = time.time() + 600  # 10 min de bloqueio
        return "🚫 Código errado 3x. Bloqueado por 10 minutos."
    return f"❌ Código errado ({pend['tries']}/{VERIFY_MAX_TRIES}). Tenta de novo."


def is_blocked(user_id: int) -> bool:
    until = BLOCKED.get(user_id, 0)
    if time.time() < until:
        return True
    if until:
        BLOCKED.pop(user_id, None)
    return False


# ----------------------------------------------------------------------------
# HELPERS DE UI
# ----------------------------------------------------------------------------

def agent_keyboard() -> InlineKeyboardMarkup:
    row = []
    buttons = []
    for name in AGENTS:
        buttons.append(InlineKeyboardButton(name, callback_data=f"agent:{name}"))
        if len(buttons) == 2:
            row.append(buttons)
            buttons = []
    if buttons:
        row.append(buttons)
    return InlineKeyboardMarkup(row)


# ----------------------------------------------------------------------------
# HANDLERS DE COMANDO
# ----------------------------------------------------------------------------

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id if update.effective_user else 0
    status = "✅ Verificado" if is_verified(user_id) else "⛔ Precisa verificar"
    await ctx.bot.send_message(
        chat_id,
        "🧠 Cérebro online.\n\n"
        "Manda qualquer mensagem que eu respondo via OpenCode.\n"
        f"Seu status: {status}\n\n"
        "Escolha uma persona:",
        reply_markup=agent_keyboard(),
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
        await ctx.bot.send_message(chat_id, "Escolha uma persona:", reply_markup=agent_keyboard())
        return
    name = args[0].lower()
    if name not in AGENTS:
        await ctx.bot.send_message(chat_id, f"Agente '{name}' não existe. Tenta: {', '.join(AGENTS)}")
        return
    st = chat_state(chat_id)
    st["agent"] = name
    st["session_id"] = None
    persist_chats()
    await ctx.bot.send_message(chat_id, f"🤖 Persona trocada para <b>{name}</b>: {AGENTS[name]['desc']}", parse_mode="HTML")


async def cmd_model(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    model = " ".join(ctx.args).strip()
    if not model:
        cur = chat_state(chat_id)["model"] or "padrão do OpenCode"
        await ctx.bot.send_message(chat_id, f"Modelo atual: <code>{cur}</code>\nUse: /model provider/modelo", parse_mode="HTML")
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
    busy = "ocupado" if LOCK.locked() else "livre"
    await ctx.bot.send_message(
        chat_id,
        f"⚙️ <b>Status</b>\n"
        f"Shift: <code>{SHIFT_NAME}</code>\n"
        f"Uptime: {hh}h{mm:02d}m{ss:02d}s\n"
        f"Cérebro: {busy}\n"
        f"Agente: <code>{st['agent']}</code>\n"
        f"Modelo: <code>{st['model'] or 'padrão'}</code>\n"
        f"Memória do chat: {len(st['history'])} msgs\n"
        f"Tokens no shift: {st['tokens']:,} · ${st['cost']:.4f}\n"
        f"Sessão opencode: {'ativa' if st['session_id'] else 'nova'}",
        parse_mode="HTML",
    )


async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    tot_t, tot_c = 0, 0.0
    for cid, st in CHATS.items():
        tot_t += st["tokens"]
        tot_c += st["cost"]
    st = chat_state(chat_id)
    lines = [
        f"📊 <b>Estatísticas do shift</b> ({SHIFT_NAME})",
        f"Total tokens: {tot_t:,}",
        f"Custo total: ${tot_c:.4f}",
        f"Chats ativos: {len(CHATS)}",
        f"",
        f"Este chat: {st['tokens']:,} tokens · ${st['cost']:.4f}",
    ]
    await ctx.bot.send_message(chat_id, "\n".join(lines), parse_mode="HTML")


async def cmd_ping(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await ctx.bot.send_message(update.effective_chat.id, "pong 🫀")


async def cmd_broadcast(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id if update.effective_user else 0
    if user_id not in ALLOWED_IDS:
        await ctx.bot.send_message(chat_id, "⛔ Só o admin.")
        return
    msg = " ".join(ctx.args).strip()
    if not msg:
        await ctx.bot.send_message(chat_id, "Uso: /broadcast <mensagem>")
        return
    targets = set()
    for cid in list(CHATS.keys()):
        targets.add(cid)
    for uid in MEMORY.get("verified", []):
        targets.add(uid)
    targets.discard(0)
    ok = 0
    for t in targets:
        try:
            await ctx.bot.send_message(t, f"📣 {msg}")
            ok += 1
        except Exception:
            pass
    await ctx.bot.send_message(chat_id, f"📣 Enviado pra {ok} chats.")


async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    if data.startswith("agent:"):
        name = data.split(":", 1)[1]
        chat_id = query.message.chat_id
        st = chat_state(chat_id)
        st["agent"] = name
        st["session_id"] = None
        persist_chats()
        await query.message.reply_text(
            f"🤖 Persona trocada para <b>{name}</b>: {AGENTS.get(name, {}).get('desc', '')}",
            parse_mode="HTML",
        )


# ----------------------------------------------------------------------------
# MENSAGEM PRINCIPAL
# ----------------------------------------------------------------------------

async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    global QUEUE_POS
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id if update.effective_user else 0
    text = (update.effective_message.text or "").strip()

    if not text:
        return
    if is_blocked(user_id):
        await ctx.bot.send_message(chat_id, "🚫 Você foi bloqueado por excesso de tentativas de verificação.")
        return

    # ---- VERIFICAÇÃO ANTI-BOT ----
    if VERIFY_REQUIRED and not is_verified(user_id):
        pend = VERIFY.get(user_id)
        if pend:  # usuário tentando responder o código
            msg = try_verify(user_id, text)
            if msg is None:
                await ctx.bot.send_message(chat_id, "✅ Verificado! Pode usar o cérebro.")
                text = pend.get("message") or text
                if not text:
                    return
            else:
                await ctx.bot.send_message(chat_id, msg)
                return
        else:
            code = start_verify(user_id, text)
            await ctx.bot.send_message(
                chat_id,
                "🤖 <b>Verificação anti-bot</b>\n\n"
                f"Pra usar o cérebro, responda com o código:\n\n<code>{code}</code>\n\n"
                f"Expira em {VERIFY_TTL}s. {VERIFY_MAX_TRIES} tentativas.",
                parse_mode="HTML",
            )
            return

    # ---- FILA (1 cérebro por vez) ----
    if LOCK.locked():
        QUEUE_POS += 1
        pos = QUEUE_POS
        await ctx.bot.send_message(chat_id, f"⏳ Cérebro ocupado. Sua mensagem entrou na fila (posição {pos}).")
    else:
        pos = 0

    st = chat_state(chat_id)

    # ---- GRUPOS: só responde se mencionar ou responder o bot ----
    chat_type = update.effective_chat.type if update.effective_chat else None
    if chat_type in (ChatType.GROUP, ChatType.SUPERGROUP):
        is_mention = BOT_USERNAME and f"@{BOT_USERNAME}" in text
        is_reply_to_bot = (
            update.effective_message.reply_to_message
            and update.effective_message.reply_to_message.from_user
            and update.effective_message.reply_to_message.from_user.is_bot
        )
        if not (is_mention or is_reply_to_bot):
            return

    # ---- CONTEXTO DE REPLY ----
    quoted = ""
    reply_msg = update.effective_message.reply_to_message
    if reply_msg and reply_msg.text:
        quoted = f"\n[Citando: {reply_msg.text[:1000]}]\n"

    prompt = f"{quoted}{text}" if st["session_id"] else seed_prompt(st, f"{quoted}{text}")

    async def typing_loop():
        while True:
            try:
                await ctx.bot.send_chat_action(chat_id, ChatAction.TYPING)
            except Exception:
                pass
            await asyncio.sleep(10)

    t_task = asyncio.create_task(typing_loop())
    answer, err, stats = "", None, {}
    try:
        async with LOCK:
            QUEUE_POS = max(0, QUEUE_POS - 1) if pos else QUEUE_POS
            answer, new_session, err, stats = await asyncio.to_thread(
                run_bridge, prompt, st["agent"], st["model"], st["session_id"], chat_id
            )
            if (err and not answer) and "travou" not in (err or ""):
                log.info("retry 1 por falha transitória: %s", err)
                answer, new_session, err, stats = await asyncio.to_thread(
                    run_bridge, prompt, st["agent"], st["model"], st["session_id"], chat_id
                )
            if new_session:
                st["session_id"] = new_session
    finally:
        t_task.cancel()

    if err and not answer:
        await ctx.bot.send_message(chat_id, f"💥 <b>Erro no cérebro:</b>\n<code>{err[:1500]}</code>", parse_mode="HTML")
        return

    st["tokens"] += int(stats.get("tokens", 0) or 0)
    st["cost"] += float(stats.get("cost", 0) or 0)
    st["history"].append({"role": "user", "text": text})
    st["history"].append({"role": "assistant", "text": answer})
    log_conversation(chat_id, "user", text)
    log_conversation(chat_id, "assistant", answer)
    persist_chats()

    for chunk in split_reply(answer):
        await ctx.bot.send_message(chat_id, chunk)


# ----------------------------------------------------------------------------
# LIFECYCLE
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
    global BOT_USERNAME
    me = await app.bot.get_me()
    BOT_USERNAME = me.username or ""
    await app.bot.set_my_commands(
        [
            ("start", "Iniciar / escolher persona"),
            ("agent", "Trocar persona"),
            ("agents", "Listar personas"),
            ("model", "Trocar modelo"),
            ("reset", "Limpar memória"),
            ("status", "Status"),
            ("stats", "Estatísticas"),
            ("broadcast", "Enviar pra todos (admin)"),
            ("ping", "Ping"),
        ]
    )
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
    while True:
        await asyncio.sleep(MEMORY_PUSH_MINUTES * 60)
        persist_chats()
        await asyncio.to_thread(git_push)
        log.info("memória enviada ao repo")


async def heartbeat_loop() -> None:
    while True:
        await asyncio.sleep(HEARTBEAT_MINUTES * 60)
        await notify(f"🫀 shift <code>{SHIFT_NAME}</code> vivo")


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
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    log.info("iniciando polling...")
    app.run_polling(stop_signals=[signal.SIGTERM, signal.SIGINT])

    try:
        final_flush()
    except Exception as e:
        log.error("erro no flush final: %s", e)
    asyncio.run(notify(f"🔴 <b>Shift <code>{SHIFT_NAME}</code> encerrado.</b>\nVolto no próximo turno."))


if __name__ == "__main__":
    main()
