# Loop Replay Phase 2 — проект, не реализация

25.09.2026. Основа — [Phase 1](README.md#offline-loop-replay-phase-1) и
[карта покрытия](COVERAGE.md). Production thresholds, grammar, queue size,
browser ownership и timeout значения не менять ради сценариев.

## Цель и граница

Прогонять многошаговые GPT-сессии через существующий `main.listen(runtime=...)`,
настоящие FreshAudioQueue, CommandSession, GPTModes и Vosk. FakeBridge сообщает
результаты внешних операций; переходы, опросы, Send, TTS и возвраты решает только
GPTModes. Не моделировать DOM, Selenium, разрешения сайта или отдельную state machine.

Текущая Phase 1 задаёт один результат на имя action. Этого достаточно для open/
cancel, но недостаточно для разных poll двух туров: production читает `composer`,
`send`, `recording`, `reply_ready` и `answer`. Текущий validator не принимает
полный такой сценарий. Нельзя просто ставить `reply_ready=true` без корректного
результата: fake нарушит контракт, а не воспроизведёт ошибку приложения.

## Предлагаемый формат

Отдельный внешний файл со `schema_version: 2`; v1 продолжает работать без миграции.
Это предложение, **нынешний CLI его не принимает**. Пример фрагмента:

```json
{
  "schema_version": 2,
  "scenarios": [{
    "id": "anton-two-turns",
    "duration": 110,
    "steps": [
      {"clip": "wake-mikhail-01", "at": "initial_waiting"},
      {"clip": "anton-open-01", "at": "wake_ack.playback_end"},
      {"clip": "local-time-01", "at": "phase.dictating", "occurrence": 1},
      {"clip": "local-date-01", "at": "phase.dictating", "occurrence": 2},
      {"clip": "atomic-return-planned-01", "at": "phase.dictating", "occurrence": 3}
    ],
    "bridge_script": [
      {"action": "open_anton", "occurrence": 1, "complete_after_ms": 50, "fixture": "anton_ready"},
      {"action": "dictate", "occurrence": 1, "complete_after_ms": 50, "fixture": "dictating"},
      {"action": "transcribe", "occurrence": 1, "complete_after_ms": 50, "fixture": "dictation_stopped"},
      {"action": "poll", "phase": "transcribing", "occurrence": 1, "fixture": "transcript_ready_1"},
      {"action": "send", "occurrence": 1, "fixture": "sent"},
      {"action": "poll", "phase": "reply", "occurrence": 1, "fixture": "reply_ready_1"}
    ],
    "expected": {
      "action_counts": {"open_anton": 1, "send": 2, "close": 1},
      "cleanup_confirmed": true,
      "min_settle_ms": 2000
    }
  }]
}
```

Пример сокращён: исполняемый сценарий обязан объявить второй тур, третью
диктовку, close и правила всех разрешённых poll. WAV LOCAL используются здесь
только как speech energy для диктовки; fake transcription НЕ считается
распознаванием браузером этих вопросов. Эталон ответа — нейтральная repo fixture,
не извлечение пользовательской переписки.

`bridge_script` сопоставляет action, ordinal в текущей generation и при необходимости
наблюдаемую phase. Fixture возвращает строго типизированный production result.
Неизвестный action, лишний Send, недостающий fixture и конфликт правил — FAIL.
Для регулярного poll разрешить явное конечное правило: состояние fixture до
объявленного времени, затем следующий fixture, с `max_calls`. Poll не двигает
GPTModes напрямую. Ordinal/phase — проверка запроса, не команда перехода.

Outcome каждой записи — ровно один из `result`, безопасный код `error`, `pending`,
`late_result`; дополнительно ограниченный delay. Повторяемость только для poll;
Send/микрофонные side effects автоматически не повторять. Поздние Future должны
реально завершаться после close/new generation, чтобы проверять production
изоляцию, а не заранее отбрасывать событие внутри fake.

## Минимальные сценарии

| Группа | Последовательность / проверяемый эффект |
|---|---|
| ANTON два тура | Wake → open → dictate → пауза PCM → transcribe → poll → Send → reply → FakeSpeech lifecycle → новая dictate, два раза; затем atomic и отдельно two-step return |
| LARISA control | Open → voice; guard active блокирует Vosk, затем достоверная тишина позволяет control wake/atomic; pause → «Слушаю» → return → confirmed close |
| Pending open | Atomic раньше completion; поздний успех не включает режим; close один |
| Hung poll | Pending Future не останавливает tick/watchdog; bounded close, нет позднего Send |
| Transcription timeout | Poll не даёт готовый composer; production deadline закрывает/обрабатывает ошибку; Send=0 |
| Unknown Send | Send мог произойти, Future pending/error; возврат закрывает, Send=1, итог unknown; поздний reply не звучит |
| Reply timeout | Poll остаётся незавершённым/без reply_ready; production timeout, нет старого ответа в новой сессии |
| Guard unavailable/failed | Запрещённые PCM не идут в Vosk, tick работает; ограниченный аварийный путь |
| Cleanup failure | Close error/unconfirmed: LOCAL не объявлен, новый open запрещён; fake не приравнивает вызов close к подтверждению |
| Control matrix | Все реальные WAKE/RETURN/ATOMIC_RETURN WAV, expected из внешнего manifest; отдельные negative samples |

Для multi-turn добавить observer metadata turn/utterance ordinal, completion и
число chunks без текста ответа. Нельзя делать assert на fake state вместо
production событий/actions. FakeSpeech не создаёт microphone PCM.

## Файлы и минимальные изменения будущего этапа

- Новый `replay_bridge_script.py`: strict schema и последовательности результатов,
  небольшой набор синтетических fixtures; никаких shell/Python полей JSON.
- Изменить только harness `replay_support.py`, `loop_replay.py` для подключения
  script и отчёта; новые `test_loop_multiturn.py`, `examples/loop-multiturn.json`.
- Сохранить Runtime seam. Production не требует переписывания. Только если
  существующих событий недостаточно, отдельно обосновать no-op observer event.
- Сейчас duration ограничена 120 с. Для реальных 180/240-секундных deadline-тестов
  будущий v2 должен разрешить больший конечный virtual horizon с ограничением
  событий и запасом на close/settle. Не уменьшать production timeouts и не
  использовать реальные sleep. Runtime создаётся заново для каждого сценария.

## Приёмка и риски

Каждый обязательный сценарий выполняет полный input и объявленное settle window;
успешный Send или вход в режим не завершают наблюдение. Проверяются точные
counts, порядок, запрет преждевременного LOCAL, отсутствие late/duplicate actions.
Прогон с deliberately wrong/late duplicate fixture должен FAIL.

Разделять REAL-VOICE control, SYNTHETIC dictation/reply, unit script-contract и LIVE.
Один fake result не доказывает UI Stop, транскрипцию ChatGPT или реальный ответ.
Не хранить answer/composer в observer/report, только fixture ID и статус.
Модель одна на прогон, expected не вычисляется actual matcher. Все WAV и новые
scenario-файлы внешние. Existing 51/0/4 и unit должны не регрессировать.

Риски: слишком «умный» fake обходит production переход; последовательность poll
зависит от выбранного шага scheduler; voice может не endpoint'нуться до истечения
окна. Лечить явными finite contracts и воспроизводящим тестом, не alias/threshold
подгонкой. Известный SHOULD FIX Phase 1: перед будущими длительными прогонами
заморозить config/source snapshot на входе и проверить его на выходе; текущий
`dirty_fingerprint` охватывает восемь source-файлов, а не весь worktree/модель.
