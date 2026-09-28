# Проверки: команды и границы

Из корня проекта, существующая `.venv`, без sudo. Это справочник, не указание
запускать все команды сейчас. Следующий обязательный шаг только в
[ROADMAP.md](ROADMAP.md). Карта заменяемых ручных проверок — [COVERAGE.md](COVERAGE.md).

## Без физических устройств

| Проверка | Команда | Что доказывает / не доказывает |
|---|---|---|
| Unit | `timeout --kill-after=20s 600s .venv/bin/python -m unittest -q` | Логика, synthetic audio, управляемые process tests. Opt-in браузер/Vosk-silence считаются отдельно; это не live голос |
| ASR real corpus | `timeout --kill-after=20s 600s .venv/bin/python audio_replay.py --corpus ~/mb-test-audio --mode asr` | Настоящий Vosk/grammar на неизменённых WAV, streaming endpoint. Не listen/timing/микрофон |
| Loop | `timeout --kill-after=20s 900s .venv/bin/python loop_replay.py --corpus ~/mb-test-audio --scenarios ~/mb-test-audio/loop-scenarios.json` | Настоящий listen/GPTModes + Vosk, внешняя среда fake; четыре onset cases пока NOT RUN |
| Matrix | `timeout --kill-after=20s 900s .venv/bin/python loop_replay.py --corpus ~/mb-test-audio --matrix all` | Все supported clips из manifest, counts и settle; не точный speech onset и не браузер |
| Совместный loop + matrix | `timeout --kill-after=20s 900s .venv/bin/python loop_replay.py --corpus ~/mb-test-audio --scenarios ~/mb-test-audio/loop-scenarios.json --matrix all` | Текущий baseline 51/0/4; NOT RUN не превращается в PASS |
| Onset candidates | `.venv/bin/python suggest_onsets.py --corpus ~/mb-test-audio` | Только heuristic RMS; не подтверждённая разметка и не изменение WAV |
| Wake perf offline | `timeout --kill-after=20s 120s .venv/bin/python wake_perf.py --count 20` | Одна Model, создание backend и первый Accept на искусственной тишине. Не live responsiveness; существующий результат достаточен для текущего прохода |
| Статика | `git diff --check` | Whitespace/conflicts diff, не функциональность |

`--report ~/mb-test-audio/reports/NEW-NAME.json` у ASR/loop/onset/perf сохраняет
новый внешний отчёт. Выбирать новое имя: существующие отчёты не перезаписываются.
Без `--report` файл не создаётся. Личные WAV/manifest/reports не добавлять в Git.
Для точечного loop: `--scenario ID` или `--tag TAG`; detailed trace —
`--debug-trace` и внешний report, без PCM. Loop exit ненулевой для required
FAIL/NOT RUN; не интерпретировать 51/0/4 как выполненные 55 случаев.

После изменения Python: `py_compile` только изменённых/новых Python-файлов.
После изменения только Markdown эти команды не запускают повторный unit suite.
Внешний timeout — последний предел, не доказательство cleanup; process tests
имеют собственный teardown/учёт потомков, остатки проверять отдельно.

## Только по согласованной живой задаче

| Проверка | Команда | Среда / предел доказательства |
|---|---|---|
| Wake perf live | `.venv/bin/python -u wake_perf.py --live` | Запускает настоящий main, микрофон и RHVoice; пользователь выполняет оговорённые команды. Perf не заменяет субъективную оценку; обычные LOCAL ASR logs остаются |
| Recorder | `.venv/bin/python record_test_corpus.py --corpus ~/mb-test-audio --resume` | Физические mic/RHVoice, по 3 take; человек выбирает retry/keep/skip. Не тренирует Vosk. `--list` лишь показывает фразы |
| Browser opt-in | `MB_BROWSER_TEST=1 timeout --kill-after=20s 300s .venv/bin/python -m unittest test_browser_dom -v` | Последовательно настоящий Firefox на локальных fixtures, focus/PID/lifecycle. Без ChatGPT; не сеть/permissions/Voice |
| Resource measurement | `.venv/bin/python measure_resources.py --pid "${MB_PID:?Укажите PID проверяемого приложения}" --seconds 20 --label local` | Read-only CPU/RSS дерева уже работающего процесса; operator заранее проверяет PID. Не PSS, не latency, не полный acceptance |

Не запускать browser suites параллельно. Resource measurement не запускает сам
ассистент и не меняет систему; системные swap/zram метрики относятся ко всей машине.
Обычный Firefox пользователя не закрывать. Сейчас ни одна физическая/браузерная
команда из таблицы не выполняется автоматически.
