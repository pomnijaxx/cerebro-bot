#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CEREBRO-BOT v3 — Bot de Telegram com cérebro OpenCode
=======================================================
Bridging: mensagens do Telegram -> OpenCode (subprocesso) -> resposta de volta.
Roda em turnos de 5h30 dentro do GitHub Actions (limite hard de 6h por job).

Comandos (36):
  /start          Iniciar + inline keyboard de personas
  /help           Lista completa de comandos
  /agent <nome>   Trocar persona (helper/rp/uncensored/hacker)
  /agents         Listar personas disponíveis
  /agentinfo <n>  Ver system prompt de uma persona
  /model <m>      Trocar modelo (provider/model)
  /reset          Limpar memória + sessão deste chat
  /remember <txt> Salvar um fato permanente
  /forget <n>     Remover fato pelo índice
  /memories       Listar fatos permanentes
  /history        Últimas 10 mensagens
  /summary        Resumir conversa via opencode
  /context        Ver o que é injetado nos prompts
  /settings       Todas as configurações do chat
  /lang <código>  Definir idioma da resposta (pt/en/es...)
  /name <nome>    Dar um nome pro bot neste chat
  /system <txt>   Adicionar instrução custom ao system prompt
  /status         Status do cérebro (uptime, agente, modelo, fila)
  /stats          Tokens e custo acumulados
  /id             Seu user_id + chat_id
  /about          Sobre o bot
  /uptime         Tempo ligado
  /next           Próximo turno
  /time           Hora UTC atual + tempo restante do turno
  /who            Chats ativos (admin)
  /broadcast <m>  Enviar msg pra todos (admin)
  /flush          Forçar save + push agora (admin)
  /shutdown       Desligar bot graciosamente (admin)
  /block <id>     Bloquear usuário (admin)
  /unblock <id>   Desbloquear (admin)
  /session <id>   Setar sessão manualmente (admin/debug)
  /echo <txt>     Ecoa a mensagem
  /roll [NdM]     Rolar dados (ex: /roll 2d6)
  /coin           Cara ou coroa
  /choose a,b,c   Escolher aleatoriamente
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

# ============================================================================
# CONFIG
# ============================================================================

BASE_DIR = Path(__file__).resolve().parent.parent

try:
    from dotenv import load_dotenv
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
BRANCH = os.environ.get("GITHUB_REF_NAME", "master")

AGENTS = json.loads((BASE_DIR / "bot" / "agents.json").read_text(encoding="utf-8"))

MAX_HISTORY = 40
SEED_MESSAGES = 12
MAX_REPLY = 4000
MAX_FACTS = 20
COOLDOWN_SEC = 2
UPTIME_START = time.time()

SHIFT_CRON = {
    "turno-a": {"start": [0, 12], "end_min": 330, "next": "turno-b"},
    "turno-b": {"start": [5.5, 17.5], "end_min": 330, "next": "turno-a"},
}

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


# ============================================================================
# MEMÓRIA PERSISTENTE
# ============================================================================

def load_memory() -> dict:
    if MEMORY_FILE.exists():
        try:
            return json.loads(MEMORY_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"chats": {}, "verified": [], "blocked": []}


def save_memory(mem: dict) -> None:
    MEMORY_FILE.write_text(json.dumps(mem, ensure_ascii=False, indent=1), encoding="utf-8")


def git_push(retries: int = 2) -> None:
    cmds = [
        ["git", "config", "user.name", "cerebro-bot"],
        ["git", "config", "user.email", "cerebro-bot@users.noreply.github.com"],
        ["git", "add", "memory.json"],
        ["git", "commit", "-m", f"chore: snapshot {now_iso()} [skip ci]"],
        ["git", "pull", "--rebase", "origin", BRANCH],
        ["git", "push", "origin", BRANCH],
    ]
    for _ in range(retries):
        ok = True
        for cmd in cmds:
            r = subprocess.run(cmd, capture_output=True, text=True, cwd=BASE_DIR)
            if r.returncode != 0 and "nothing to commit" not in (r.stdout + r.stderr):
                if cmd[1] == "commit":
                    continue
                ok = False
                break
        if ok:
            return
        time.sleep(3)


# ============================================================================
# PONTE OPencode
# ============================================================================

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def parse_stream(stdout: str):
    texts, seen, session_id, error = [], set(), None, None
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
            p = ev.get("part") or {}
            if p.get("type") == "text":
                pid = p.get("id")
                if pid in seen:
                    continue
                seen.add(pid)
                txt = ANSI_RE.sub("", p.get("text", "")).strip()
                if txt:
                    texts.append(txt)
        elif t == "step_finish":
            p = ev.get("part") or {}
            tk = p.get("tokens") or {}
            stats["tokens"] += int(tk.get("total", 0) or 0)
            stats["input"] += int(tk.get("input", 0) or 0)
            stats["output"] += int(tk.get("output", 0) or 0)
            stats["cost"] += float(p.get("cost", 0) or 0)
        elif t == "error":
            err = ev.get("error") or {}
            error = err.get("message") if isinstance(err, dict) else str(err)
    return "\n".join(texts).strip(), session_id, error, stats


def run_bridge(prompt, agent, model, session_id, chat_id):
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
        return "", session_id, f"tempo esgotado ({BRIDGE_TIMEOUT}s)", {}
    except FileNotFoundError:
        return "", session_id, "opencode não encontrado", {}
    answer, new_session, err, stats = parse_stream(proc.stdout)
    if not answer and not err:
        tail = (ANSI_RE.sub("", proc.stderr or "").strip().splitlines()[-3:])
        err = "\n".join(tail) or f"sem resposta (exit {proc.returncode})"
    return answer or "", new_session or session_id, err, stats


# ============================================================================
# ESTADO POR CHAT
# ============================================================================

MEMORY = load_memory()
CHATS: dict[int, dict] = {}
LOCK = asyncio.Lock()
COOLDOWNS: dict[int, float] = {}
VERIFY: dict[int, dict] = {}
BOT_USERNAME: str = ""
QUEUE_POS: int = 0
SHUTDOWN_EVENT = None


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
            "lang": raw.get("lang", "pt"),
            "persona_name": raw.get("persona_name", ""),
            "system_prompt": raw.get("system_prompt", ""),
            "facts": list(raw.get("facts", [])[:MAX_FACTS]),
            "title": raw.get("title", ""),
        }
    return CHATS[chat_id]


def persist_all() -> None:
    chats = {}
    for cid, st in CHATS.items():
        chats[str(cid)] = {
            "agent": st["agent"],
            "model": st["model"],
            "history": list(st["history"])[-MAX_HISTORY:],
            "tokens": st["tokens"],
            "cost": st["cost"],
            "lang": st["lang"],
            "persona_name": st["persona_name"],
            "system_prompt": st["system_prompt"],
            "facts": st["facts"],
            "title": st["title"],
        }
    save_memory({
        "chats": chats,
        "verified": list(MEMORY.get("verified", [])),
        "blocked": list(MEMORY.get("blocked", [])),
    })


def build_prefix(st: dict) -> str:
    """Monta o prefixo que é injetado em toda mensagem."""
    parts = []
    lang = st.get("lang", "pt")
    if lang:
        parts.append(f"Responda em {lang}.")
    name = st.get("persona_name", "")
    if name:
        parts.append(f"Você é {name}.")
    sys = st.get("system_prompt", "")
    if sys:
        parts.append(sys)
    facts = st.get("facts", [])
    if facts:
        parts.append("Fatos permanentes:\n" + "\n".join(f"- {f}" for f in facts))
    return "\n".join(parts)


def build_prompt(st: dict, text: str, quoted: str = "") -> str:
    prefix = build_prefix(st)
    history_seed = ""
    if not st["session_id"]:
        hist = [h for h in st["history"] if h.get("text")]
        if hist:
            lines = ["[Memória de conversas anteriores:]"]
            for h in hist[-SEED_MESSAGES:]:
                lines.append(f"{h['role']}: {h['text'][:1200]}")
            lines.append("[Fim da memória]")
            history_seed = "\n".join(lines)
    parts = [p for p in [prefix, history_seed, quoted, text] if p]
    return "\n\n".join(parts)


def split_reply(text: str) -> list[str]:
    if len(text) <= MAX_REPLY:
        return [text]
    chunks, cur = [], ""
    for para in text.split("\n\n"):
        if len(cur) + len(para) + 2 > MAX_REPLY:
            if cur:
                chunks.append(cur)
            cur = para
        else:
            cur = f"{cur}\n\n{para}" if cur else para
    if cur:
        chunks.append(cur)
    return chunks


def log_conversation(chat_id: int, role: str, text: str) -> None:
    try:
        with (CONV_DIR / f"{chat_id}.md").open("a", encoding="utf-8") as f:
            f.write(f"\n## {now_iso()} [{role}]\n{text[:3000]}\n")
    except Exception:
        pass


# ============================================================================
# VERIFICAÇÃO ANTI-BOT
# ============================================================================

def is_verified(uid: int) -> bool:
    return uid in ALLOWED_IDS or uid in MEMORY.get("verified", [])


def mark_verified(uid: int) -> None:
    if uid not in ALLOWED_IDS:
        v = MEMORY.setdefault("verified", [])
        if uid not in v:
            v.append(uid)


def start_verify(uid: int, msg: str) -> str:
    code = str(secrets.randbelow(10000)).zfill(4)
    VERIFY[uid] = {"code": code, "tries": 0, "expires": time.time() + VERIFY_TTL, "message": msg}
    return code


def try_verify(uid: int, text: str) -> str | None:
    p = VERIFY.get(uid)
    if not p:
        return None
    if time.time() > p["expires"]:
        VERIFY.pop(uid, None)
        return "⏰ Código expirado. Manda uma mensagem pra ganhar um novo."
    if text.strip() == p["code"]:
        mark_verified(uid)
        VERIFY.pop(uid, None)
        return None
    p["tries"] += 1
    if p["tries"] >= VERIFY_MAX_TRIES:
        VERIFY.pop(uid, None)
        MEMORY.setdefault("blocked", []).append(uid)
        return "🚫 3 tentativas falhadas. Bloqueado por 10 min."
    return f"❌ Código errado ({p['tries']}/{VERIFY_MAX_TRIES}). Tenta de novo."


def is_blocked(uid: int) -> bool:
    return uid in MEMORY.get("blocked", [])


# ============================================================================
# UI HELPERS
# ============================================================================

def agent_keyboard() -> InlineKeyboardMarkup:
    row, rows = [], []
    for name in AGENTS:
        row.append(InlineKeyboardButton(f"🤖 {name}", callback_data=f"agent:{name}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


def admin_check(update: Update) -> bool:
    uid = update.effective_user.id if update.effective_user else 0
    return uid in ALLOWED_IDS


def time_remaining() -> str:
    elapsed = time.time() - UPTIME_START
    remaining = max(0, 330 * 60 - elapsed)
    h, m = int(remaining // 3600), int((remaining % 3600) // 60)
    return f"{h}h{m:02d}m"


# ============================================================================
# COMANDOS (36)
# ============================================================================

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    uid = update.effective_user.id if update.effective_user else 0
    st = chat_state(chat_id)
    st["title"] = update.effective_chat.title or update.effective_chat.username or ""
    v = "✅ Verificado" if is_verified(uid) else "🔒 Verificação pendente"
    await ctx.bot.send_message(
        chat_id,
        f"🧠 <b>Cérebro online</b>\n\n"
        f"Status: {v}\n"
        f"Persona: <code>{st['agent']}</code>\n"
        f"Modelo: <code>{st['model'] or 'padrão'}</code>\n\n"
        f"Escolha uma persona ou manda qualquer mensagem:",
        reply_markup=agent_keyboard(),
        parse_mode="HTML",
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await ctx.bot.send_message(
        update.effective_chat.id,
        "🧠 <b>CEREBRO-BOT — Comandos</b>\n\n"
        "<b>🧠 Cérebro</b>\n"
        "/agent &lt;nome&gt; — trocar persona\n"
        "/agents — listar personas\n"
        "/agentinfo &lt;nome&gt; — ver system prompt\n"
        "/model &lt;provider/model&gt; — trocar modelo\n"
        "/reset — limpar memória + sessão\n\n"
        "<b>💾 Memória</b>\n"
        "/remember &lt;texto&gt; — salvar fato permanente\n"
        "/forget &lt;n&gt; — remover fato #n\n"
        "/memories — listar fatos\n"
        "/history — últimas 10 msgs\n"
        "/summary — resumo da conversa\n"
        "/context — ver o que é injetado nos prompts\n\n"
        "<b>⚙️ Configuração</b>\n"
        "/lang &lt;código&gt; — idioma (pt/en/es...)\n"
        "/name &lt;nome&gt; — nome do bot\n"
        "/system &lt;texto&gt; — instrução custom\n"
        "/settings — ver todas as configs\n\n"
        "<b>📊 Info</b>\n"
        "/status — status do cérebro\n"
        "/stats — tokens e custo\n"
        "/id — seus IDs\n"
        "/uptime — tempo ligado\n"
        "/next — próximo turno\n"
        "/time — hora atual + restante\n"
        "/about — sobre o bot\n\n"
        "<b>🎮 Diversão</b>\n"
        "/roll [NdM] — dados (ex: /roll 2d6)\n"
        "/coin — cara ou coroa\n"
        "/choose a,b,c — escolher um\n"
        "/echo &lt;txt&gt; — ecoar mensagem\n\n"
        "<b>👑 Admin</b>\n"
        "/broadcast &lt;msg&gt; — enviar pra todos\n"
        "/who — chats ativos\n"
        "/flush — save + push agora\n"
        "/shutdown — desligar bot\n"
        "/block &lt;id&gt; — bloquear\n"
        "/unblock &lt;id&gt; — desbloquear\n"
        "/session &lt;id&gt; — setar sessão",
        parse_mode="HTML",
    )


async def cmd_agent(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if not ctx.args:
        await ctx.bot.send_message(chat_id, "Escolha uma persona:", reply_markup=agent_keyboard())
        return
    name = ctx.args[0].lower()
    if name not in AGENTS:
        await ctx.bot.send_message(chat_id, f"❌ '{name}' não existe. Opções: {', '.join(AGENTS)}")
        return
    st = chat_state(chat_id)
    st["agent"] = name
    st["session_id"] = None
    persist_all()
    await ctx.bot.send_message(
        chat_id,
        f"🤖 <b>Persona → {name}</b>\n{AGENTS[name]['desc']}\n\nModelo: <code>{st['model'] or 'padrão'}</code>",
        parse_mode="HTML",
    )


async def cmd_agents(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    lines = ["🧠 <b>Personas disponíveis:</b>\n"]
    for name, info in AGENTS.items():
        lines.append(f"• <b>{name}</b> — {info['desc']}")
    lines.append(f"\nUse /agent &lt;nome&gt; pra trocar.")
    await ctx.bot.send_message(update.effective_chat.id, "\n".join(lines), parse_mode="HTML")


async def cmd_agentinfo(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not ctx.args:
        await ctx.bot.send_message(update.effective_chat.id, "Uso: /agentinfo <nome>")
        return
    name = ctx.args[0].lower()
    fpath = BASE_DIR / ".opencode" / "agent" / f"{name}.md"
    if not fpath.exists():
        await ctx.bot.send_message(update.effective_chat.id, f"❌ Agent '{name}' não encontrado.")
        return
    content = fpath.read_text(encoding="utf-8")
    match = re.match(r"^---\n(.+?)\n---\n(.*)", content, re.DOTALL)
    header = match.group(1).strip() if match else ""
    body = match.group(2).strip() if match else content
    await ctx.bot.send_message(
        update.effective_chat.id,
        f"🤖 <b>{name}</b>\n\n<b>Config:</b>\n{header}\n\n<b>System prompt:</b>\n{body[:3500]}",
        parse_mode="HTML",
    )


async def cmd_model(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    model = " ".join(ctx.args).strip()
    if not model:
        cur = chat_state(chat_id)["model"] or "padrão do OpenCode (big-pickle)"
        await ctx.bot.send_message(chat_id, f"Modelo atual: <code>{cur}</code>\n\nUse /model provider/modelo\nEx: /model openrouter/deepseek/deepseek-chat-v3-0324:free", parse_mode="HTML")
        return
    st = chat_state(chat_id)
    st["model"] = model
    st["session_id"] = None
    persist_all()
    await ctx.bot.send_message(chat_id, f"🧬 Modelo → <code>{model}</code>", parse_mode="HTML")


async def cmd_reset(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    st = chat_state(update.effective_chat.id)
    st["history"].clear()
    st["session_id"] = None
    persist_all()
    await ctx.bot.send_message(update.effective_chat.id, "🧹 Memória + sessão zeradas. Recomeçando do zero.")


async def cmd_remember(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    text = " ".join(ctx.args).strip() if ctx.args else ""
    if not text:
        await ctx.bot.send_message(update.effective_chat.id, "Uso: /remember <fato importante>")
        return
    st = chat_state(update.effective_chat.id)
    if len(st["facts"]) >= MAX_FACTS:
        st["facts"].pop(0)
    st["facts"].append(text)
    persist_all()
    await ctx.bot.send_message(update.effective_chat.id, f"💾 Fato salvo ({len(st['facts'])}/{MAX_FACTS}): <code>{text}</code>", parse_mode="HTML")


async def cmd_forget(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not ctx.args:
        await ctx.bot.send_message(update.effective_chat.id, "Uso: /forget <número do fato>\nUse /memories pra ver os números.")
        return
    st = chat_state(update.effective_chat.id)
    try:
        idx = int(ctx.args[0]) - 1
        removed = st["facts"].pop(idx)
        persist_all()
        await ctx.bot.send_message(update.effective_chat.id, f"🗑 Removido: <code>{removed}</code>", parse_mode="HTML")
    except (ValueError, IndexError):
        await ctx.bot.send_message(update.effective_chat.id, "❌ Índice inválido. Use /memories pra ver os números.")


async def cmd_memories(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    facts = chat_state(update.effective_chat.id)["facts"]
    if not facts:
        await ctx.bot.send_message(update.effective_chat.id, "Nenhum fato salvo. Use /remember <texto>.")
        return
    lines = ["💾 <b>Fatos permanentes:</b>\n"]
    for i, f in enumerate(facts, 1):
        lines.append(f"{i}. {f}")
    await ctx.bot.send_message(update.effective_chat.id, "\n".join(lines), parse_mode="HTML")


async def cmd_history(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    hist = list(chat_state(update.effective_chat.id)["history"])
    if not hist:
        await ctx.bot.send_message(update.effective_chat.id, "Nenhuma mensagem ainda.")
        return
    lines = ["📜 <b>Últimas mensagens:</b>\n"]
    for h in hist[-10:]:
        role = "👤" if h["role"] == "user" else "🤖"
        txt = h["text"][:120].replace("\n", " ")
        lines.append(f"{role} {txt}")
    await ctx.bot.send_message(update.effective_chat.id, "\n".join(lines), parse_mode="HTML")


async def cmd_summary(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    hist = chat_state(chat_id)["history"]
    if not hist:
        await ctx.bot.send_message(chat_id, "Nada pra resumir ainda.")
        return
    convo = "\n".join(f"{h['role']}: {h['text'][:500]}" for h in hist[-20:])
    prompt = f"Resuma esta conversa em 3-5 pontos-chave, em português:\n\n{convo}"
    await ctx.bot.send_chat_action(chat_id, ChatAction.TYPING)
    st = chat_state(chat_id)
    async with LOCK:
        answer, _, err, _ = await asyncio.to_thread(
            run_bridge, prompt, st["agent"], st["model"], None, chat_id
        )
    if err and not answer:
        await ctx.bot.send_message(chat_id, f"💥 Erro ao resumir: <code>{err[:500]}</code>", parse_mode="HTML")
        return
    await ctx.bot.send_message(chat_id, f"📝 <b>Resumo:</b>\n\n{answer}", parse_mode="HTML")


async def cmd_context(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    st = chat_state(update.effective_chat.id)
    parts = []
    if st["lang"]:
        parts.append(f"🌐 Idioma: {st['lang']}")
    if st["persona_name"]:
        parts.append(f"📛 Nome: {st['persona_name']}")
    if st["system_prompt"]:
        parts.append(f"⚙️ System: {st['system_prompt'][:200]}")
    if st["facts"]:
        parts.append(f"💾 Fatos: {len(st['facts'])}")
    parts.append(f"🤖 Persona: {st['agent']}")
    parts.append(f"🧬 Modelo: {st['model'] or 'padrão'}")
    parts.append(f"📜 História: {len(st['history'])} msgs")
    parts.append(f"🔒 Sessão: {'ativa' if st['session_id'] else 'nova'}")
    await ctx.bot.send_message(update.effective_chat.id, "🔍 <b>Contexto injetado:</b>\n\n" + "\n".join(parts), parse_mode="HTML")


async def cmd_lang(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    lang = " ".join(ctx.args).strip() if ctx.args else ""
    if not lang:
        cur = chat_state(update.effective_chat.id)["lang"]
        await ctx.bot.send_message(update.effective_chat.id, f"Idioma atual: <code>{cur}</code>\n\nUse /lang pt (ou en/es/fr/de...)", parse_mode="HTML")
        return
    st = chat_state(update.effective_chat.id)
    st["lang"] = lang
    st["session_id"] = None
    persist_all()
    await ctx.bot.send_message(update.effective_chat.id, f"🌐 Idioma → <code>{lang}</code>", parse_mode="HTML")


async def cmd_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    name = " ".join(ctx.args).strip() if ctx.args else ""
    if not name:
        cur = chat_state(update.effective_chat.id)["persona_name"] or "(nenhum)"
        await ctx.bot.send_message(update.effective_chat.id, f"Nome atual: <code>{cur}</code>\n\nUse /name <nome>", parse_mode="HTML")
        return
    st = chat_state(update.effective_chat.id)
    st["persona_name"] = name
    persist_all()
    await ctx.bot.send_message(update.effective_chat.id, f"📛 Nome → <code>{name}</code>", parse_mode="HTML")


async def cmd_system(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    text = " ".join(ctx.args).strip() if ctx.args else ""
    if not text:
        cur = chat_state(update.effective_chat.id)["system_prompt"] or "(nenhuma)"
        await ctx.bot.send_message(update.effective_chat.id, f"Instrução atual: <code>{cur[:300]}</code>\n\nUse /system <instrução>", parse_mode="HTML")
        return
    st = chat_state(update.effective_chat.id)
    st["system_prompt"] = text
    st["session_id"] = None
    persist_all()
    await ctx.bot.send_message(update.effective_chat.id, f"⚙️ System prompt → <code>{text}</code>", parse_mode="HTML")


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    st = chat_state(update.effective_chat.id)
    up = int(time.time() - UPTIME_START)
    hh, mm, ss = up // 3600, (up % 3600) // 60, up % 60
    busy = "🔴 ocupado" if LOCK.locked() else "🟢 livre"
    await ctx.bot.send_message(
        update.effective_chat.id,
        f"⚙️ <b>Status</b>\n\n"
        f"Shift: <code>{SHIFT_NAME}</code>\n"
        f"Uptime: {hh}h{mm:02d}m{ss:02d}s\n"
        f"Cérebro: {busy}\n"
        f"Tempo restante: {time_remaining()}\n\n"
        f"<b>Este chat:</b>\n"
        f"Persona: <code>{st['agent']}</code>\n"
        f"Modelo: <code>{st['model'] or 'padrão'}</code>\n"
        f"Idioma: <code>{st['lang']}</code>\n"
        f"Fatos: {len(st['facts'])}\n"
        f"História: {len(st['history'])} msgs\n"
        f"Tokens: {st['tokens']:,} · ${st['cost']:.4f}\n"
        f"Sessão: {'ativa' if st['session_id'] else 'nova'}",
        parse_mode="HTML",
    )


async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    tot_t = sum(s["tokens"] for s in CHATS.values())
    tot_c = sum(s["cost"] for s in CHATS.values())
    st = chat_state(update.effective_chat.id)
    await ctx.bot.send_message(
        update.effective_chat.id,
        f"📊 <b>Estatísticas</b> ({SHIFT_NAME})\n\n"
        f"Total: {tot_t:,} tokens · ${tot_c:.4f}\n"
        f"Chats: {len(CHATS)}\n\n"
        f"Este chat: {st['tokens']:,} tokens · ${st['cost']:.4f}",
        parse_mode="HTML",
    )


async def cmd_id(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    c = update.effective_chat
    await ctx.bot.send_message(
        update.effective_chat.id,
        f"🆔 <b>ID</b>\n\n"
        f"User ID: <code>{u.id}</code>\n"
        f"Chat ID: <code>{c.id}</code>\n"
        f"Chat tipo: {c.type}\n"
        f"Username: @{u.username or 'N/A'}\n"
        f"Verificado: {'✅' if is_verified(u.id) else '❌'}",
        parse_mode="HTML",
    )


async def cmd_about(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await ctx.bot.send_message(
        update.effective_chat.id,
        "🧠 <b>Cérebro-Bot v3</b>\n\n"
        "Bot de Telegram cujo cérebro é o OpenCode.\n"
        "Roda em turnos de 5h30 no GitHub Actions.\n"
        "Memória persistente via git commits.\n"
        "4 personas · 36 comandos · anti-bot verify.\n\n"
        "Github: <code>pomnijaxx/cerebro-bot</code>",
        parse_mode="HTML",
    )


async def cmd_uptime(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    up = int(time.time() - UPTIME_START)
    d, up = divmod(up, 86400)
    h, up = divmod(up, 3600)
    m, s = divmod(up, 60)
    parts = []
    if d:
        parts.append(f"{d}d")
    parts.append(f"{h}h{m:02d}m{s:02d}s")
    await ctx.bot.send_message(update.effective_chat.id, f"⏱ Uptime: {' '.join(parts)}\nRestante do turno: {time_remaining()}")


async def cmd_next(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    now = datetime.now(timezone.utc)
    hour = now.hour + now.minute / 60
    info = SHIFT_CRON.get(SHIFT_NAME, {})
    starts = info.get("start", [0])
    next_starts = [s for s in starts if s > hour]
    if not next_starts:
        next_hour = starts[0] + 24
    else:
        next_hour = next_starts[0]
    diff_h = next_hour - hour
    diff_m = int(diff_h * 60)
    nh, nm = divmod(diff_m, 60)
    next_shift = info.get("next", "?")
    await ctx.bot.send_message(
        update.effective_chat.id,
        f"⏭ Próximo turno: <code>{next_shift}</code>\n"
        f"Começa em: {nh}h{nm:02d}m\n"
        f"Horário: {int(next_hour) % 24:02d}:{int((next_hour % 1) * 60):02d} UTC",
        parse_mode="HTML",
    )


async def cmd_time(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    now = datetime.now(timezone.utc)
    await ctx.bot.send_message(
        update.effective_chat.id,
        f"🕐 UTC: <code>{now.strftime('%H:%M:%S')}</code>\n"
        f"Shift: <code>{SHIFT_NAME}</code>\n"
        f"Restante: {time_remaining()}",
        parse_mode="HTML",
    )


async def cmd_who(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not admin_check(update):
        return
    if not CHATS:
        await ctx.bot.send_message(update.effective_chat.id, "Nenhum chat ativo.")
        return
    lines = [f"👥 <b>{len(CHATS)} chats ativos:</b>\n"]
    for cid, st in CHATS.items():
        t = st.get("title", "") or str(cid)
        lines.append(f"• {t} ({cid}) — {st['agent']} — {len(st['history'])} msgs")
    await ctx.bot.send_message(update.effective_chat.id, "\n".join(lines), parse_mode="HTML")


async def cmd_broadcast(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not admin_check(update):
        return
    msg = " ".join(ctx.args).strip()
    if not msg:
        await ctx.bot.send_message(update.effective_chat.id, "Uso: /broadcast <mensagem>")
        return
    targets = set(CHATS.keys()) | set(MEMORY.get("verified", []))
    targets.discard(0)
    ok = 0
    for t in targets:
        try:
            await ctx.bot.send_message(t, f"📣 {msg}")
            ok += 1
        except Exception:
            pass
    await ctx.bot.send_message(update.effective_chat.id, f"📣 Enviado pra {ok} chats.")


async def cmd_flush(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not admin_check(update):
        return
    persist_all()
    await asyncio.to_thread(git_push)
    await ctx.bot.send_message(update.effective_chat.id, "💾 Memória salva e enviada ao repo.")


async def cmd_shutdown(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not admin_check(update):
        return
    await ctx.bot.send_message(update.effective_chat.id, "🔴 Desligando...")
    persist_all()
    await asyncio.to_thread(git_push)
    if SHUTDOWN_EVENT:
        SHUTDOWN_EVENT.set()


async def cmd_block(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not admin_check(update):
        return
    if not ctx.args:
        await ctx.bot.send_message(update.effective_chat.id, "Uso: /block <user_id>")
        return
    try:
        uid = int(ctx.args[0])
        blk = MEMORY.setdefault("blocked", [])
        if uid not in blk:
            blk.append(uid)
        persist_all()
        await ctx.bot.send_message(update.effective_chat.id, f"🚫 <code>{uid}</code> bloqueado.", parse_mode="HTML")
    except ValueError:
        await ctx.bot.send_message(update.effective_chat.id, "ID inválido.")


async def cmd_unblock(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not admin_check(update):
        return
    if not ctx.args:
        await ctx.bot.send_message(update.effective_chat.id, "Uso: /unblock <user_id>")
        return
    try:
        uid = int(ctx.args[0])
        blk = MEMORY.get("blocked", [])
        if uid in blk:
            blk.remove(uid)
            persist_all()
            await ctx.bot.send_message(update.effective_chat.id, f"✅ <code>{uid}</code> desbloqueado.", parse_mode="HTML")
        else:
            await ctx.bot.send_message(update.effective_chat.id, f"<code>{uid}</code> não estava bloqueado.", parse_mode="HTML")
    except ValueError:
        await ctx.bot.send_message(update.effective_chat.id, "ID inválido.")


async def cmd_session(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not admin_check(update):
        return
    if not ctx.args:
        st = chat_state(update.effective_chat.id)
        await ctx.bot.send_message(update.effective_chat.id, f"Sessão atual: <code>{st['session_id'] or 'nenhuma'}</code>", parse_mode="HTML")
        return
    st = chat_state(update.effective_chat.id)
    st["session_id"] = ctx.args[0]
    await ctx.bot.send_message(update.effective_chat.id, f"Sessão → <code>{ctx.args[0]}</code>", parse_mode="HTML")


async def cmd_echo(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    text = " ".join(ctx.args) if ctx.args else "..."
    await ctx.bot.send_message(update.effective_chat.id, text)


async def cmd_roll(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    spec = ctx.args[0] if ctx.args else "1d6"
    match = re.match(r"(\d+)d(\d+)", spec.lower())
    if not match:
        await ctx.bot.send_message(update.effective_chat.id, "Uso: /roll [NdM] (ex: /roll 2d6)")
        return
    n, m = int(match.group(1)), int(match.group(2))
    n = min(n, 100)
    m = min(m, 1000)
    rolls = [random.randint(1, m) for _ in range(n)]
    total = sum(rolls)
    detail = ", ".join(str(r) for r in rolls)
    await ctx.bot.send_message(update.effective_chat.id, f"🎲 {spec}: {detail}\nTotal: <b>{total}</b>", parse_mode="HTML")


async def cmd_coin(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    result = random.choice(["🪙 Cara", "🪙 Coroa"])
    await ctx.bot.send_message(update.effective_chat.id, result)


async def cmd_choose(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    text = " ".join(ctx.args) if ctx.args else ""
    opts = [o.strip() for o in text.split(",") if o.strip()]
    if len(opts) < 2:
        await ctx.bot.send_message(update.effective_chat.id, "Uso: /choose opção1, opção2, opção3")
        return
    chosen = random.choice(opts)
    await ctx.bot.send_message(update.effective_chat.id, f"🎯 Escolhi: <b>{chosen}</b>", parse_mode="HTML")


# ============================================================================
# CALLBACKS
# ============================================================================

async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    if data.startswith("agent:"):
        name = data.split(":", 1)[1]
        if name not in AGENTS:
            return
        st = chat_state(q.message.chat_id)
        st["agent"] = name
        st["session_id"] = None
        persist_all()
        await q.message.reply_text(
            f"🤖 Persona → <b>{name}</b>: {AGENTS[name]['desc']}",
            parse_mode="HTML",
        )


# ============================================================================
# MENSAGEM PRINCIPAL
# ============================================================================

async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    global QUEUE_POS
    chat_id = update.effective_chat.id
    uid = update.effective_user.id if update.effective_user else 0
    text = (update.effective_message.text or "").strip()
    if not text:
        return

    if is_blocked(uid):
        await ctx.bot.send_message(chat_id, "🚫 Você está bloqueado.")
        return

    st = chat_state(chat_id)
    st["title"] = update.effective_chat.title or st.get("title", "")

    # ANTI-BOT
    if VERIFY_REQUIRED and not is_verified(uid):
        pend = VERIFY.get(uid)
        if pend:
            msg = try_verify(uid, text)
            if msg is None:
                await ctx.bot.send_message(chat_id, "✅ Verificado! Manda sua mensagem.")
                text = pend.get("message") or text
                if not text:
                    return
            else:
                await ctx.bot.send_message(chat_id, msg)
                return
        else:
            code = start_verify(uid, text)
            await ctx.bot.send_message(
                chat_id,
                f"🤖 <b>Verificação anti-bot</b>\n\n"
                f"Responda com o código:\n<code>{code}</code>\n\n"
                f"Expira em {VERIFY_TTL}s · {VERIFY_MAX_TRIES} tentativas.",
                parse_mode="HTML",
            )
            return

    # GRUPOS: só responde se mencionado ou reply
    chat_type = update.effective_chat.type if update.effective_chat else None
    if chat_type in (ChatType.GROUP, ChatType.SUPERGROUP):
        mentioned = BOT_USERNAME and f"@{BOT_USERNAME}" in text
        replied = (
            update.effective_message.reply_to_message
            and update.effective_message.reply_to_message.from_user
            and update.effective_message.reply_to_message.from_user.is_bot
        )
        if not (mentioned or replied):
            return

    # QUOTE
    quoted = ""
    rm = update.effective_message.reply_to_message
    if rm and rm.text:
        quoted = f"[Citando: {rm.text[:800]}]"

    # FILA
    if LOCK.locked():
        QUEUE_POS += 1
        await ctx.bot.send_message(chat_id, f"⏳ Cérebro ocupado. Fila: posição {QUEUE_POS}.")

    prompt = build_prompt(st, text, quoted)

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
            QUEUE_POS = max(0, QUEUE_POS - 1)
            answer, new_session, err, stats = await asyncio.to_thread(
                run_bridge, prompt, st["agent"], st["model"], st["session_id"], chat_id
            )
            if err and not answer:
                answer, new_session, err, stats = await asyncio.to_thread(
                    run_bridge, prompt, st["agent"], st["model"], st["session_id"], chat_id
                )
            if new_session:
                st["session_id"] = new_session
    finally:
        t_task.cancel()

    if err and not answer:
        await ctx.bot.send_message(chat_id, f"💥 <b>Erro:</b>\n<code>{err[:1500]}</code>", parse_mode="HTML")
        return

    st["tokens"] += int(stats.get("tokens", 0) or 0)
    st["cost"] += float(stats.get("cost", 0) or 0)
    st["history"].append({"role": "user", "text": text})
    st["history"].append({"role": "assistant", "text": answer})
    log_conversation(chat_id, "user", text)
    log_conversation(chat_id, "assistant", answer)
    persist_all()

    for chunk in split_reply(answer):
        await ctx.bot.send_message(chat_id, chunk)


# ============================================================================
# LIFECYCLE
# ============================================================================

async def notify(text: str) -> None:
    if not ADMIN_ID:
        return
    try:
        app = Application.builder().token(TOKEN).build()
        await app.bot.send_message(ADMIN_ID, text)
        await app.shutdown()
    except Exception:
        pass


async def post_init(app: Application) -> None:
    global BOT_USERNAME, SHUTDOWN_EVENT
    SHUTDOWN_EVENT = asyncio.Event()
    me = await app.bot.get_me()
    BOT_USERNAME = me.username or ""
    await app.bot.set_my_commands([
        ("start", "Iniciar"), ("help", "Ajuda"), ("agent", "Persona"),
        ("agents", "Personas"), ("agentinfo", "Info persona"), ("model", "Modelo"),
        ("reset", "Limpar"), ("remember", "Salvar fato"), ("forget", "Remover fato"),
        ("memories", "Fatos"), ("history", "Histórico"), ("summary", "Resumo"),
        ("context", "Contexto"), ("lang", "Idioma"), ("name", "Nome"),
        ("system", "System prompt"), ("status", "Status"), ("stats", "Stats"),
        ("id", "IDs"), ("about", "Sobre"), ("uptime", "Uptime"),
        ("next", "Próximo turno"), ("time", "Hora"), ("who", "Chats"),
        ("broadcast", "Broadcast"), ("flush", "Flush"), ("shutdown", "Desligar"),
        ("block", "Bloquear"), ("unblock", "Desbloquear"), ("session", "Sessão"),
        ("echo", "Ecoar"), ("roll", "Dados"), ("coin", "Moeda"),
        ("choose", "Escolher"), ("roll", "Dados"),
    ])
    if MEMORY_PUSH_MINUTES > 0:
        asyncio.get_running_loop().create_task(memory_loop())
    if HEARTBEAT_MINUTES > 0 and ADMIN_ID:
        asyncio.get_running_loop().create_task(heartbeat_loop())
    asyncio.get_running_loop().create_task(shutdown_waiter())
    model_desc = DEFAULT_MODEL or "padrão (big-pickle)"
    await notify(f"🟢 <b>Shift <code>{SHIFT_NAME}</code> online</b>\nModelo: <code>{model_desc}</code>\n36 comandos · anti-bot verify ativo")


async def memory_loop() -> None:
    while True:
        await asyncio.sleep(MEMORY_PUSH_MINUTES * 60)
        persist_all()
        await asyncio.to_thread(git_push)
        log.info("memória enviada ao repo")


async def heartbeat_loop() -> None:
    while True:
        await asyncio.sleep(HEARTBEAT_MINUTES * 60)
        await notify(f"🫀 shift <code>{SHIFT_NAME}</code> vivo")


async def shutdown_waiter() -> None:
    if SHUTDOWN_EVENT:
        await SHUTDOWN_EVENT.wait()
    log.info("shutdown sinalizado via /shutdown")
    os.kill(os.getpid(), signal.SIGTERM)


def final_flush() -> None:
    persist_all()
    git_push()
    log.info("flush final")


def main() -> None:
    if not TOKEN:
        log.error("TELEGRAM_BOT_TOKEN não definido")
        sys.exit(1)
    app = Application.builder().token(TOKEN).post_init(post_init).build()
    for cmd, handler in [
        ("start", cmd_start), ("help", cmd_help), ("agent", cmd_agent),
        ("agents", cmd_agents), ("agentinfo", cmd_agentinfo), ("model", cmd_model),
        ("reset", cmd_reset), ("remember", cmd_remember), ("forget", cmd_forget),
        ("memories", cmd_memories), ("history", cmd_history), ("summary", cmd_summary),
        ("context", cmd_context), ("lang", cmd_lang), ("name", cmd_name),
        ("system", cmd_system), ("status", cmd_status), ("stats", cmd_stats),
        ("id", cmd_id), ("about", cmd_about), ("uptime", cmd_uptime),
        ("next", cmd_next), ("time", cmd_time), ("who", cmd_who),
        ("broadcast", cmd_broadcast), ("flush", cmd_flush), ("shutdown", cmd_shutdown),
        ("block", cmd_block), ("unblock", cmd_unblock), ("session", cmd_session),
        ("echo", cmd_echo), ("roll", cmd_roll), ("coin", cmd_coin),
        ("choose", cmd_choose),
    ]:
        app.add_handler(CommandHandler(cmd, handler))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    log.info("iniciando polling...")
    app.run_polling(stop_signals=[signal.SIGTERM, signal.SIGINT])
    final_flush()
    asyncio.run(notify(f"🔴 Shift <code>{SHIFT_NAME}</code> encerrado. Volto no próximo turno."))


if __name__ == "__main__":
    main()
