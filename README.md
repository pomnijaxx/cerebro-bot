# 🧠 Cerebro-Bot

Bot de Telegram cujo **cérebro é o OpenCode** rodando em VM do **GitHub Actions**.
O bot lê as mensagens do Telegram, manda pro OpenCode, captura o que ele responde
e replica no chat. Com **agentes** (personas) de RP, uncensored, hacker etc.

Roda em **turnos de 5h30** porque o GitHub Actions tem limite hard de **6h por job**
(360 min). Quando um turno termina, o outro workflow assume — e assim vai, o dia inteiro,
de graça (repo público = minutos ilimitados).

---

## Arquitetura

```
Telegram ──mensagem──▶ bot.py ──prompt──▶ opencode run (subprocesso)
   ▲                                        │  modelo (gateway free ou sua key)
   │                                        ▼
   └──────────────resposta────────◀─ parse NDJSON ──┘
```

- **`bot/bot.py`** — bot (python-telegram-bot) + ponte com o OpenCode.
  Cada mensagem vira um `opencode run --format json`; o bot parseia os eventos
  `text` e devolve a resposta. Sessões (`--session`) dão memória dentro do turno.
- **`.opencode/agent/*.md`** — personas que o OpenCode carrega (`--agent <nome>`).
- **`.github/workflows/turno-a.yml` / `turno-b.yml`** — as VMs. Cada uma roda 5h30 e morre.
- **`memory.json`** — memória dos chats. O bot commita de volta no repo a cada 15 min,
  então **a memória sobrevive entre turnos** (a VM é nova a cada shift, o git não).

### Grade de turnos (UTC)

| Workflow | Cron          | Roda            | Termina |
|----------|---------------|-----------------|---------|
| turno-a  | `0 0,12 * * *` | 00:00 e 12:00   | 05:30 e 17:30 |
| turno-b  | `30 5,17 * * *`| 05:30 e 17:30   | 11:00 e 23:00 |

Cobertura ≈ **22h/dia**. Gaps de 1h às 11:00 e 23:00 (limite de 6h por job não permite
cobrir 100%). Quer fechar o gap? Adicione um terceiro workflow com cron `0 11,23 * * *`
rodando 55 min — o template é o mesmo dos outros.

---

## Setup

### 1. Crie o bot no Telegram
Fale com [@BotFather](https://t.me/BotFather): `/newbot` → pegue o **token**.

### 2. Pegue seu ID e o auth do OpenCode (gateway free)
- Seu ID numérico: fale com [@userinfobot](https://t.me/userinfobot).
- Auth do gateway free (modelos `opencode/*` como `opencode/deepseek-v4-flash-free`):
  ```bash
  opencode auth login        # local, escolha o provider "opencode" (ou outro)
  base64 -w0 ~/.local/share/opencode/auth.json   # copie a saída
  ```
  Se preferir usar sua própria chave (OpenRouter/Anthropic/OpenAI/Gemini), pule este
  passo e configure só a key no secrets.

### 3. Crie o repo e suba o projeto
```bash
git init && git add . && git commit -m "cerebro-bot"
git remote add origin https://github.com/<seu-user>/cerebro-bot.git
git push -u origin main
```
> **Repo público** = Actions grátis. Privado = 2000 min/mês não aguentam turnos de 5h30.

### 4. Secrets do repo
`Settings → Secrets and variables → Actions`:

| Secret | Obrigatório | Valor |
|--------|-------------|-------|
| `TELEGRAM_BOT_TOKEN` | ✅ | token do BotFather |
| `ADMIN_ID` | ✅ | seu id numérico (recebe avisos de shift) |
| `ALLOWED_IDS` | ➖ | ids permitidos, vírgula-separados. **Vazio = qualquer um pode usar seu cérebro.** |
| `OPENCODE_AUTH_B64` | ➖ | base64 do auth.json (gateway free). Sem isso, configure uma key abaixo |
| `OPENROUTER_API_KEY` | ➖ | se for usar modelos OpenRouter |
| `ANTHROPIC_API_KEY` | ➖ | se for usar Claude |
| `OPENAI_API_KEY` | ➖ | se for usar GPT |
| `GEMINI_API_KEY` | ➖ | se for usar Gemini |

### 5. Vars do repo (mesma tela, aba Variables)
| Var | Default | Descrição |
|-----|---------|-----------|
| `DEFAULT_MODEL` | (vazio) | modelo do cérebro. Ex: `opencode/deepseek-v4-flash-free` (free), `openrouter/deepseek/deepseek-chat-v3-0324:free`, `anthropic/claude-...` |
| `DEFAULT_AGENT` | `helper` | persona padrão de chats novos |

### 6. Rode manualmente
Abra **Actions → Turno A → Run workflow**. O bot deve te mandar
"🟢 Shift turno-a online" no Telegram. Depois disso os crons assumem.

---

## Comandos do bot

| Comando | Efeito |
|---------|--------|
| `/agent <nome>` | troca persona (`helper`, `rp`, `uncensored`, `hacker`) |
| `/agents` | lista personas |
| `/model <provider/modelo>` | troca modelo na hora |
| `/reset` | zera a memória do chat |
| `/status` | shift, uptime, agente, modelo, memória |
| `/ping` | pong 🫀 |

---

## Como funciona por baixo

1. Mensagem chega → bot chama `opencode run --format json --agent <X> --model <Y> --session <id> "<mensagem>"`.
2. O OpenCode responde como NDJSON: eventos `step_start`, `text` (a resposta), `step_finish`.
3. O bot junta os blocos `part.text`, manda pro Telegram (quebra em 4000 chars se preciso).
4. A cada 15 min (e no fim do turno) `memory.json` é commitado no repo. No próximo turno,
   a VM já nasce com a memória e injeta o histórico do turno anterior no 1º prompt de cada chat.

**Latência:** cada mensagem é uma chamada nova ao modelo (5–30s). É um bot de
conversa, não de tempo real. Mensagens em fila são processadas uma por vez (1 cérebro).

---

## Troubleshooting

- **"Erro no cérebro" / sem resposta** — veja `logs/shift.log` na aba Actions (Artifacts
  não habilitado; o log aparece no step se rodar com `--print-logs`). Causas comuns:
  sem `DEFAULT_MODEL` + sem auth → configure uma key ou o `OPENCODE_AUTH_B64`.
- **Modelo não encontrado** — rode `opencode models` localmente pra ver nomes exatos
  (formato `provider/modelo`).
- **Job morre aos 6h** — não morre: o `timeout 19800` (5h30) derruba o bot antes,
  com flush de memória e aviso de shift encerrado. Se passar de 5h30 a culpa é do
  limite de minutos ou de delay da fila do Actions.
- **Overlap de turnos** — o `concurrency` impede dois shifts ao mesmo tempo.
- **Alguém abusando do bot** — seta `ALLOWED_IDS` com seu id. Vazio = porta aberta.
- **Git push falhando** — o `GITHUB_TOKEN` já tem permissão de escrita (`contents: write`
  no workflow). Se mudar o branch padrão, ajuste `GITHUB_REF_NAME`/`BRANCH`.
- **Bot offline 1h/dia** — normal (gap de turno). Veja a tabela de cron.

---

## Custo

- **Repo público:** Actions ilimitado → R$ 0.
- **Cérebro:** gateway free do opencode (`opencode/*`) ou modelos free do OpenRouter.
  Se usar modelos pagos (Claude/GPT), a conta do provider que paga.
