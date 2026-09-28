# Loop Replay checkpoint — финальная приёмка

29.09.2026: LOCAL FreshWake live smoke **PASS**, 6/6 wake и 6/6 TIME,
0 duplicate wake, пользователь не сообщил о блокирующей задержке. Рабочий create
≈1,65–2,13 мс; общий diagnostic max 8,05 мс. Подробности и границы — в
[ACCEPTANCE.md](ACCEPTANCE.md). Python/config с последнего AUTO не изменены;
unit baseline 296/0/8 сохранён без повторного запуска. Пользователь явно разрешил
checkpoint commit и push только `feature/loop-replay`, без merge/rebase/tag.

Основание: read-only code review + документация, 25.09.2026. Ветка `feature/loop-replay`,
HEAD `9b08d3269e342aee2804305844382a4753e8b8dc`. Исходный WIP сохранён;
**production/harness/test Python в этом проходе не менялся**. Предыдущие изменения
относительно HEAD остаются частью будущего checkpoint. Результаты —
[ACCEPTANCE.md](ACCEPTANCE.md), покрытие — [COVERAGE.md](COVERAGE.md).

## Финальное review

**BLOCKER перед LOCAL live smoke: не обнаружен.** Это вывод по текущему diff,
тестовым доказательствам и контрактам, не гарантия любого поведения устройства.

| Участок | Проверенный вывод |
|---|---|
| Runtime seam | `listen(runtime=None)` создаёт ListenRuntime: настоящие clock/stream/Speech/Bridge/Guard/command. main не импортирует scheduler/fakes. Подмена явная, только через переданный runtime; callbacks остаются захватом metadata/PCM и bounded queue |
| Clock | GPTModes start/tick/step используют один injected clock; default late-bound `time.monotonic`. Capture PortAudio и monotonic не приравнены; fake audio epoch отдельный |
| FreshWake | Reset отпускает backend; factory сохраняет одну Model, lazy `_current` создаёт следующий в main thread на допущенном PCM. Callback не создаёт Vosk. Во время TTS/cooldown recognizer не вызывается. Одна initial creation плюс по одному backend на новый допущенный wake-отрезок; gap/RMS restart могут дать больше одного за длинный цикл |
| Lifetime | Python weakref/lazy tests проверяют отпускание backend/model и отсутствие retention instrumentation. Wrapper не выдаёт старый backend в listen. Native Vosk destructor освобождает recognizer; длительный memory soak не выполнен, доказательств утечки нет |
| Loop | `run_case` вызывает настоящий listen/FreshAudioQueue/CommandSession/GPTModes. Real CLI использует Vosk, tests с MarkerRecognizer отдельно помечены synthetic. Matrix expected читается из manifest, не match_intent |
| Termination | Новая matrix проходит всю duration и >=2 с settle после последнего input/значимого действия. Late duplicate и короткий settle дают FAIL. Старые `stop_at` open-сценарии доказывают только вход; это не multi-turn conversation |
| Scheduler | Heap с порядковым номером, извлечение события один раз; capture/delivery/main задержки отдельные. Max-time/max-events и duplicate-delivery/source-range invariants. FakeSpeech не создаёт PCM |
| Audio | PCM WAV поступает целиком, без hidden trim/resample/normalize. Validator общий с ASR. Onset candidate не ground truth. Corpus/manifest/scenarios не записываются harness |
| Observer | В текущих событиях scalar metadata, без PCM/ответов GPT/cookies/tokens. Запрет имён полей не универсальный редактор произвольного текста: будущие поля также требуют privacy review |
| Control | Clock/observer WIP не меняет atomic/two-step алгоритм. Pending open/Send, late generation, повторный close, новый session до cleanup покрыты atomic/process tests. Send не повторяется, LOCAL только после confirmed close |
| Browser | browser_worker/chatgpt_bridge/chatgpt_ui/speaker_activity/config не изменены относительно checkpoint этим Loop WIP. Startup/ownership/12-с cleanup не перерабатывались |

Default runtime сохраняет прежние зависимости/семантику seam. Это **не** заявление
о побайтовой эквивалентности всего main checkpoint: отдельный уже внесённый FreshWake
fix намеренно исправляет Vosk stale partial после Reset. Его live стоимость
принята узким smoke 29.09; offline ~1 мс сам по себе этот шаг не заменял.

**SHOULD FIX, не блокирует checkpoint:**

1. `loop_replay.cli/run_case`: `dirty_fingerprint` — hash восьми указанных source
   файлов, config и corpus имеют отдельные hashes, модель только identification.
   Это не полный worktree/model/dependency fingerprint. При редактировании config
   во время большого прогона Model загружается по первому чтению, а каждый case
   читает config снова; source hashes снимаются к концу. Будущий Phase 2 должен
   freeze/сверять входной snapshot. Текущие результаты не обесценены: source/config
   hashes совпали, в проверенном прогоне concurrent edits не выполнялись.
2. Phase 1 fake Bridge не является полноценным контрактом многотурового poll:
   `reply_ready=true` без answer — некорректный fixture для GPTModes.completed.
   Текущие сценарии так не делают; следующий этап обязан использовать typed
   result fixtures, см. [Phase 2](LOOP-REPLAY-PHASE2.md), а не менять production.

Не подтверждены дефекты callback blocking от Vosk creation, retention старого
backend, двойной delivery или production session revival. Новых failing
production tests для гипотез без воспроизведения не добавлялось.

## Точный состав будущего checkpoint

Это **весь накопленный WIP**, не только документы текущего прохода.

| Группа | Файлы |
|---|---|
| Production seam/fix | `main.py`, `gpt_modes.py` |
| Harness/support | `audio_replay.py`, `loop_replay.py`, `loop_matrix.py`, `replay_support.py`, `replay_trace.py`, `suggest_onsets.py`, `wake_perf.py` |
| Tests | `test_chatgpt.py`, `test_mvp.py`, `test_loop_replay.py`, `test_loop_matrix.py`, `test_replay_support.py`, `test_replay_trace.py`, `test_wake_perf.py` |
| Docs | `AGENTS.md`, `README.md`, `ROADMAP.md`, `ACCEPTANCE.md`, `COVERAGE.md`, `CHECKPOINT.md`, `TESTING.md`, `LOOP-REPLAY-PHASE2.md`, `AGENT-MODE-DESIGN.md`, `EVERYDAY-DESIGN.md`, `PERFORMANCE-BACKLOG.md` |
| Example | `examples/loop-scenarios.json` |

`record_test_corpus.py`, `browser_timing.py` и прочие checkpoint dependencies уже
tracked и не требуют повторного добавления без diff. Все новые импортируемые
replay-модули и тесты есть в списке. Личных WAV, внешнего manifest/scenarios/reports,
logs, .venv, models, профиля, секретов и временных traces в списке нет.

**Точный разрешённый список для checkpoint после принятого live smoke:**

```bash
git add -- \
  main.py gpt_modes.py audio_replay.py \
  loop_replay.py loop_matrix.py replay_support.py replay_trace.py suggest_onsets.py wake_perf.py \
  test_chatgpt.py test_mvp.py test_loop_replay.py test_loop_matrix.py \
  test_replay_support.py test_replay_trace.py test_wake_perf.py \
  AGENTS.md README.md ROADMAP.md ACCEPTANCE.md COVERAGE.md CHECKPOINT.md TESTING.md \
  LOOP-REPLAY-PHASE2.md AGENT-MODE-DESIGN.md EVERYDAY-DESIGN.md PERFORMANCE-BACKLOG.md \
  examples/loop-scenarios.json
```

Разрешённый message: `Add deterministic loop replay and fix stale wake context`.
Перед commit проверить staged diff/check и равенство списка реальному WIP;
не включать лишние файлы. Разрешение commit/push дано пользователем 29.09.

## Release / branches — предложение, без git-операций

Локальные refs на момент review: `feature/chatgpt-voice` и `feature/loop-replay`
оба на `9b08d32`, `main` на `b1cc85d` (v0.1.1). Remote не опрашивался; вывод о
локальной `origin/*` не доказывает текущее состояние сервера. Перед будущим
merge проверить актуальные refs и ancestry повторно.

1. v0.2.1 checkpoint `9b08d32` сохраняется как историческая база; это не команда
   создания тега и не утверждение, что опубликован release tag.
2. После единственного 5× smoke — Loop Replay checkpoint на feature/loop-replay
   по явному решению пользователя, с актуальной записью LIVE.
3. Не нужен промежуточный merge loop-replay обратно в feature/chatgpt-voice:
   при сохранении текущего ancestry replay branch уже содержит voice checkpoint.
   Предпочтителен один review/PR из loop-replay в main после release gates. Если
   ветки разошлись, отдельно пересмотреть integration path, не делать reset.
4. v0.2.x reliability tag только после успешных AUTO, относящегося к изменённым
   участкам live regression, подтверждённой очистки и отдельной длительной
   проверки/замеров. Пять LOCAL циклов разрешают checkpoint, не весь release.
   Не включать невыполненный Phase 2 в обещания версии.
5. Phase 2 может разрабатываться на новой ветке от принятого replay checkpoint;
   повседневный запуск — отдельный проверяемый этап. v0.3 включает Agent Mode
   только после plan-only/validator/executor/confirmation acceptance.

Сейчас: **FINAL LOCAL LIVE SMOKE PASS; READY FOR CHECKPOINT YES**.
Следующий этап после checkpoint — Loop Replay Phase 2. Длительная работа,
GPT/browser и акустическое эхо этим LOCAL smoke не проверялись.
