#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Teste de integração: importa o bot.py e chama a ponte de verdade."""
import importlib.util
import sys

sys.path.insert(0, "/data/data/com.termux/files/home/cerebro-bot/bot")
spec = importlib.util.spec_from_file_location(
    "botmod", "/data/data/com.termux/files/home/cerebro-bot/bot/bot.py"
)
bot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bot)

print("=== Teste 1: parser NDJSON com linha simples ===")
out = (
    '{"type":"step_start","sessionID":"ses_TESTE123","part":{"type":"step-start"}}\n'
    '{"type":"text","sessionID":"ses_TESTE123","part":{"id":"p1","type":"text","text":"Olá! Eu sou o cérebro."}}\n'
    '{"type":"text","sessionID":"ses_TESTE123","part":{"id":"p2","type":"text","text":"Tudo certo por aqui."}}\n'
    '{"type":"step_finish","sessionID":"ses_TESTE123","part":{"type":"step-finish","reason":"stop"}}\n'
)
resp, sid, err = bot.parse_stream(out)
assert resp == "Olá! Eu sou o cérebro.\nTudo certo por aqui.", f"FALHOU: {resp!r}"
assert sid == "ses_TESTE123"
assert err is None
print("  parse OK ->", resp, "| session:", sid)

print("=== Teste 2: chamada REAL via opencode (agente helper) ===")
resp, sid, err = bot.run_bridge(
    prompt="Responda apenas com: BRIDGE FUNCIONANDO",
    agent="helper",
    model="opencode/deepseek-v4-flash-free",
    session_id=None,
    chat_id=999999,
)
print("  resposta:", resp)
print("  session:", sid)
print("  erro:", err)
assert "BRIDGE FUNCIONANDO" in resp, "bridge real falhou!"
assert sid, "não capturou session id"

print("=== Teste 3: continuação de sessão (memória dentro do turno) ===")
resp2, sid2, err2 = bot.run_bridge(
    prompt="Que palavra exata você disse antes? (resposta: a mesma palavra)",
    agent="helper",
    model="opencode/deepseek-v4-flash-free",
    session_id=sid,
    chat_id=999999,
)
print("  resposta2:", resp2[:200])
print("  session2:", sid2)
print("  erro2:", err2)

print("\nTODOS OS TESTES PASSARAM ✅")
