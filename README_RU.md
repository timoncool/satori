<div align="center">

# satori

**Луп самообучения для Claude Code — модель учится скиллам на собственных сессиях, за человеческим гейтом.**

[![License](https://img.shields.io/github/license/timoncool/satori?style=flat-square)](LICENSE)
[![Stars](https://img.shields.io/github/stars/timoncool/satori?style=flat-square)](https://github.com/timoncool/satori/stargazers)
[![Last Commit](https://img.shields.io/github/last-commit/timoncool/satori?style=flat-square)](https://github.com/timoncool/satori/commits)

**[English](README.md)** · **[Русский](README_RU.md)**

![satori](docs/screenshots/hero.png)

</div>

MCP-сервер + хуки, дающие Claude Code замкнутый цикл самообучения: коррекции юзера и падения тулзов превращаются в кандидаты-уроки, уроки — в драфты скиллов, драфты — в боевые скиллы, но только через гейт. Windows-native, работает в Claude Desktop, ноль bash-обёрток.

Два принципа, которых нет у аналогов: **сервер делает только детерминированную механику** (парсинг, счётчики, хранение, валидация) — думает вызывающая модель прямо в сессии, никаких фоновых LLM-вызовов и лишних биллов; и **ничего не активируется само** — драфты лежат в staging до явного одобрения.

## Возможности

- **Луп из 4 стадий** — capture → decide → distill → curate; `reflect()` дергается несколько раз за сессию
- **Коррекция юзера = сигнал №1** — всплывает кандидатом с первого раза (механика Devin); падения тулзов ждут повторов
- **Patch-not-append** — повтор сигнала бампает `seen_count`, а не плодит записи
- **SKIP-гейт навсегда** — «это не стоит скилла» помнится вечно, мусор не возвращается
- **Staging вместо автозаписи** — драфт активируется только через `approve_draft` (руками или скиллом wake)
- **Триггер — священное поле** — `description: Use when ...` обязателен: recall работает, когда записано «когда вспоминать», а не «что делает»
- **pinned_project** — урок глобален или привязан к проекту, approve сам роутит в нужную папку скиллов
- **Валидация драфтов** — frontmatter, размер, секреты (замазываются и в хранилище), prompt-injection маркеры (EN+RU)
- **FTS5-поиск по прошлым сессиям** — «когда я чинил ровно эту ошибку» находит конкретный транскрипт
- **Куратор** — телеметрия использования, невостребованное протухает за 30 дней, архивируется за 90
- **Умные nudge-хуки** — молчат по умолчанию; голос только при коррекции (мгновенно) или накопленной работе; «отказ значит отказ»
- **Видимый след в чате** — каждое срабатывание лупа = строка-маркер ⛩: что сработало, почему, что записано или скипнуто
- **Интеграция с dream/wake** — консолидация [dream-skill](https://github.com/timoncool/dream-skill) сама жнёт staging satori и выносит promote/retire драфтов через свой валидатор-гейт

## Быстрый старт

**Проще всего — пусть Клод сам и установит.** Кинь это сообщение в Claude Code:

```text
Установи луп самообучения satori из https://github.com/timoncool/satori:
1) склонируй репо в постоянное место (не времянку — MCP работает из него);
2) создай внутри venv и поставь единственную зависимость: fastmcp;
3) зарегистрируй MCP в моём .mcp.json (проектном или глобальном) как "satori":
   command = абсолютный путь к python из venv, args = [абсолютный путь к server.py];
4) рекомендуется: добавь три nudge-хука (UserPromptSubmit / Stop / SessionEnd,
   все зовут hooks/nudge.py питоном из venv — точный JSON в README, шаг 3
   Быстрого старта) в ~/.claude/settings.json, СОХРАНИВ мои существующие хуки;
5) смоук: импортни server в venv и покажи, что настроено;
и напомни перезапустить Claude Code / Desktop, чтобы MCP и хуки подгрузились.
```

Всё — Клод склонирует, пропишет конфиги, проверит и отчитается. Ручной способ:

1. **Клонируем и ставим зависимость**
   ```bash
   git clone https://github.com/timoncool/satori.git
   cd satori && python -m venv .venv && .venv\Scripts\pip install fastmcp
   ```

2. **Регистрируем MCP** — в `.mcp.json` проекта (или глобальный конфиг):
   ```jsonc
   "satori": {
     "command": "<путь>\\satori\\.venv\\Scripts\\python.exe",
     "args": ["<путь>\\satori\\server.py"]
   }
   ```

3. **(Опционально) хуки-напоминалки** — в `~/.claude/settings.json` три хука на один скрипт:
   ```jsonc
   "UserPromptSubmit": [{"matcher": "", "hooks": [{"type": "command", "command": "<venv-python> <путь>/hooks/nudge.py prompt-submit", "timeout": 10}]}],
   "Stop":             [{"matcher": "", "hooks": [{"type": "command", "command": "<venv-python> <путь>/hooks/nudge.py stop", "timeout": 10}]}],
   "SessionEnd":       [{"matcher": "", "hooks": [{"type": "command", "command": "<venv-python> <путь>/hooks/nudge.py session-end", "timeout": 60}]}]
   ```
   Перезапусти Claude Code / Desktop.

## Как это работает

```
транскрипт сессии
      │  (хук/вызов — 0 токенов на разбор)
      ▼
① capture   reflect() вычитывает новое с offset'а: коррекции, падения,
            fix-after-fail, сложные сегменты (≥12 calls + ≥2 правки)
      ▼
② decide    модель судит кандидатов (коррекции — с 1 раза, прочее — с 2):
            мусор → skip_lesson (навсегда), стоящее → драфт
      ▼
③ distill   submit_draft → staging/, валидация, бэкап, provenance.
            НЕ активируется. approve_draft → ~/.claude/skills/ или проект
      ▼
④ curate    телеметрия использования, stale 30д, архив 90д
```

**Хуки** (все опциональны, все молчат по умолчанию): `UserPromptSubmit` — при коррекции впрыскивает одну строку «почини и вызови reflect» (с дедупом серии); по наработке ≥25 calls — то же, и повторно только после следующего полного порога; `Stop` — тот же порог на конце хода; `SessionEnd` — тихий capture напрямую в питоне, вообще без модели. Когда nudge срабатывает, модель начинает ответ с видимого маркера `⛩ satori: ...` и отчитывается, что записалось — работа лупа всегда на виду.

**Анти-засирание:** в память Claude луп не пишет никогда (только своя SQLite + staging); впрыск в контекст — одна строка и только по делу; проигнорированный nudge не долбится.

## Тулзы

| Tool | Что делает |
|------|-----------|
| `reflect(transcript_path?)` | стадии 1+2+4: сигналы с offset'а, агрегация, кандидаты + похожие скиллы, тик куратора |
| `skip_lesson(key, reason)` | вечный SKIP |
| `submit_draft(name, markdown, lesson_key?, patches?, pinned_project?)` | драфт в staging с полной валидацией |
| `approve_draft(name, dest_dir?)` | гейт: staging → боевые скиллы (с роутингом по pinned_project) |
| `session_search(query, limit?)` | FTS5 по всем прошлым транскриптам |
| `loop_status()` | телеметрия лупа |

## Конфигурация (env)

| Переменная | Дефолт | Смысл |
|---|---|---|
| `SN_PROMOTE_AT` | 2 | повторов до кандидата (кроме коррекций) |
| `SN_CORRECTION_PROMOTE_AT` | 1 | коррекция юзера — с первого раза |
| `SN_SEGMENT_TOOL_CALLS` / `SN_SEGMENT_FILE_EDITS` | 12 / 2 | порог «сложного сегмента» |
| `SN_STALE_DAYS` / `SN_ARCHIVE_DAYS` | 30 / 90 | старение драфтов |
| `SN_NUDGE_MIN_CALLS` | 25 | наработка для nudge |
| `SN_NUDGE_COOLDOWN_MIN` / `SN_CORR_COOLDOWN_MIN` | 10 / 3 | кулдауны nudge |
| `SN_STOP_NUDGE` | 1 | nudge на Stop-хуке (0 = выкл) |

## Лучше всего работает вместе с dream-skill

[**dream-skill**](https://github.com/timoncool/dream-skill) — родной брат этого проекта: консолидация памяти Claude Code (dream = read-only проход, wake = применение через гейт, полный откат). satori ведёт **процедурную память** (скиллы), dream/wake — **фактическую** (заметки, правила, индекс), и они смыкаются:

- фаза **Skill harvest** у dream читает `staging/` satori + телеметрию использования и выносит `promote_skill` / `retire_skill` рядом с memory-предложениями
- гейт общий: твои галочки — или независимый агент-валидатор в авто-режиме (с обязательным чек-листом драфта: триггер `Use when`, нет injection-маркеров, нет дублей)
- wake активирует или хоронит драфты, логирует каждую активацию, и его `откати сон` возвращает и скиллы

Каждый работает сам по себе; вместе цикл полный: сон → пробуждение → прозрение.

## Откуда что взято

Честный список источников: [Hermes Agent](https://github.com/NousResearch/hermes-agent) (Nous) — 4-стадийный цикл, FTS5, саморефлексия; [claude-self-improving-skills](https://github.com/UniM0cha/claude-self-improving-skills) — пороги сложности, patch-over-create, куратор, телеметрия, «отказ значит отказ»; [claude-evolve](https://github.com/taipm/claude-evolve) — объективные сигналы, patch-not-append; [claude-harness-hermes](https://github.com/jjackkun/claude-harness-hermes) — вечный SKIP, redaction; Devin (Cognition) — коррекция с порога 1, триггер как священное поле, pinned-scoping, injection-скан; [dream-skill](https://github.com/timoncool/dream-skill) (наш) — staging-гейт.

## Другие проекты [@timoncool](https://github.com/timoncool)

| Проект | Описание |
|--------|----------|
| [dream-skill](https://github.com/timoncool/dream-skill) | Консолидация памяти Claude Code — сон/пробуждение с гейтом |
| [trail-spec](https://github.com/timoncool/trail-spec) | TRAIL — cross-MCP протокол отслеживания контента |
| [telegram-api-mcp](https://github.com/timoncool/telegram-api-mcp) | Полный Telegram Bot API как MCP сервер |
| [civitai-mcp-ultimate](https://github.com/timoncool/civitai-mcp-ultimate) | Civitai API как MCP сервер |
| [GitLife](https://github.com/timoncool/gitlife) | Твоя жизнь в неделях — интерактивный календарь |

## Авторы

- **Nerual Dreming** — [Telegram](https://t.me/nerual_dreming) | [neuro-cartel.com](https://neuro-cartel.com) | [ArtGeneration.me](https://artgeneration.me)

## Поддержать автора

Я делаю open-source софт и AI-ресёрч. Большинство того, что я создаю — бесплатно и доступно всем. Ваши донаты помогают мне продолжать творить, не думая о том, где взять деньги на следующий обед =)

**[Все способы поддержки](https://github.com/timoncool/ACE-Step-Studio/blob/master/DONATE.md)** | **[dalink.to/nerual_dreming](https://dalink.to/nerual_dreming)** | **[boosty.to/neuro_art](https://boosty.to/neuro_art)**

- **BTC:** `1E7dHL22RpyhJGVpcvKdbyZgksSYkYeEBC`
- **ETH (ERC20):** `0xb5db65adf478983186d4897ba92fe2c25c594a0c`
- **USDT (TRC20):** `TQST9Lp2TjK6FiVkn4fwfGUee7NmkxEE7C`

## Star History

<a href="https://github.com/timoncool/satori/stargazers">
 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="docs/stars-dark.svg" />
   <source media="(prefers-color-scheme: light)" srcset="docs/stars-light.svg" />
   <img alt="Star History Chart" src="docs/stars-light.svg" />
 </picture>
</a>

## Лицензия

[MIT](LICENSE)
