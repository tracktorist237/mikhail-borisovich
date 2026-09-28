# Карта проверок перед Loop Replay checkpoint

Состояние: 25.09.2026, `feature/loop-replay`, WIP поверх `9b08d32`.
Результаты и история — в [ACCEPTANCE.md](ACCEPTANCE.md), команды —
в [TESTING.md](TESTING.md). Это карта границ проверки, не новый запуск тестов.

**AUTO** заменяет ручное повторение указанной программной проверки на известных
входах. **PARTIAL AUTO** проверяет логику, но оставляет физический/UI участок.
**LIVE** означает, что основной проверяемый эффект требует реальной среды.
Исторический LIVE PASS не переносится на новый WIP автоматически.

| Проверка | Класс | Автоматическое свидетельство | Что остаётся за границей |
|---|---|---|---|
| Wake recognition записанных фраз | AUTO | `audio_replay`; matrix `matrix-wake-*`, все wake variants | Новая речь, расстояние, шум и микрофон |
| LOCAL intents записанных фраз | AUTO | `matrix-local-*`; `test_mvp.IntentTests`; батарея на fake sysfs | Реальное изменение громкости/состояние устройства; executor в loop подставной |
| Команда сразу после «Слушаю» | PARTIAL AUTO | `test_local_ready`; `LoopTests.test_real_loop_components_and_first_post_boundary_block`; clip-start matrix | Четыре точных onset случая NOT RUN; реальный конец playback и акустический хвост |
| Callback delay | AUTO | `local-callback-*`; `test_callback_delivery_does_not_shift_capture_or_pcm` | Виртуальная задержка, не измерение драйвера |
| Queue pressure | AUTO | `local-overflow-then-fresh`; `test_main_delay_keeps_callbacks_running_and_real_queue_overflows` | Причины задержек ОС под живой нагрузкой |
| Stale / gap | AUTO | `test_resilience.QueueConcurrencyTests`; `test_explicit_gap_resets_partial_and_fresh_command_still_works` | Не все возможные аппаратные разрывы |
| Duplicate wake после Reset | AUTO | `test_wake_reset_cannot_reuse_backend_feature_context_after_time`; real matrix; trace regression | Известная регрессия закрыта; не гарантия отсутствия ложного wake на любой записи |
| ANTON open | PARTIAL AUTO | `matrix-open-anton-*`; настоящий GPTModes, fake `open_anton` | Firefox, ChatGPT и диктовка физически не открываются |
| LARISA open | PARTIAL AUTO | `matrix-open-larisa-*`; fake `open` | Voice UI, разрешения и сервер |
| Atomic cancel pending open | PARTIAL AUTO | `matrix-atomic-*`; `test_atomic_return`; process `test_atomic_cancel_hung_open_and_send_through_real_supervisor` | Слышимость команды при реальной загрузке; текущий браузерный UI |
| Two-step return | PARTIAL AUTO | `matrix-two-step-*`; `test_existing_two_step_still_pauses` | Реальное выключение Voice/диктовки и слышимость «Слушаю» |
| Pending Send isolation | AUTO | `test_send_outcome_unknown_and_never_repeated`; `test_timeout_closes_pending_send_without_dispatching_late_result` | Проверяется отсутствие повторного действия; результат отправки серверу может быть неизвестен |
| Cleanup Future ordering | AUTO | `test_reap_ack_does_not_confirm_close_before_supervisor_exit`; `test_unconfirmed_close_keeps_gpt_out_of_local_mode` | Управляемые процессы и Future, не разрешения сайта |
| Browser process reaping | PARTIAL AUTO | `test_bridge_shutdown`; исторический opt-in `BrowserLifecycleTests` | Реальные doubles + прежний локальный Firefox fixture; текущий live браузер не запускался |
| Real Firefox startup | PARTIAL AUTO | Исторический `test_bridge_closes_and_restarts_real_firefox` на локальной странице | Успех загрузки ChatGPT, логин и сеть |
| ChatGPT dictation Stop | PARTIAL AUTO | Исторический `test_os_space_uses_prepared_dom_focus`, отрицательные PID/focus tests | Локальная DOM-кнопка не доказывает завершение записи текущим ChatGPT |
| Реальный ответ ChatGPT | LIVE | Fake reply проверяет только дальнейшую обработку, Phase 2 ещё не реализована | Сетевой/смысловой ответ и его озвучивание |
| Larisa microphone permissions/UI | PARTIAL AUTO | `test_voice_startup`; исторический `test_microphone_rendered_controls_one_click_and_ambiguity` | Настоящие разрешения и изменения UI сайта |
| Реальный PipeWire capture | LIVE | Подставные capture outcomes в unit | Фактический поток браузера в пользовательской аудиосессии |
| Acoustic echo | LIVE | Только синтетические запрещённые по времени PCM в boundary tests | Комната, колонки, акустический хвост |
| Physical mic sensitivity | LIVE | Известные WAV распознаются | Текущее расстояние, уровень и устройство |
| Real Celeron responsiveness | LIVE | Offline create mean 0,986 мс; виртуальный loop | Слышимая задержка под нагрузкой Firefox/ОС |
| Ctrl+C cleanup | PARTIAL AUTO | `test_sigint_shutdown.TerminalShutdownTests`: PTY, foreground pipeline, потеря stdout | Физический input stream и реальный браузер в этом WIP; прежний LIVE сохранён отдельно |
| Long-duration stability | LIVE | Короткие конечные сценарии и bounded cleanup | Длительный native RSS, ресурсы и повторные сессии |

Итого: **8 AUTO / 10 PARTIAL AUTO / 6 LIVE**, 24 проверки.
Числа описывают классы, а не количество выполненных тестов.

После программных изменений сначала повторять относящийся AUTO-набор.
Не просить заново вручную проверять все известные фразы, искусственные callback
задержки, вытеснение очереди и Future ordering. Для этого есть corpus и tests.
PARTIAL/LIVE повторяются при изменениях соответствующего участка либо release gate;
они не образуют список обязательных ручных действий перед каждым checkpoint.

**Финальный LOCAL smoke принят 29.09.2026:** шесть циклов «Михаил» → сразу
после окончания «Слушаю» «Который час», 6 wake/6 TIME, без duplicate wake;
блокирующая субъективная задержка не сообщена. Это узкий LIVE PASS, не изменение
границ AUTO в таблице. Разметка onset остаётся отдельной будущей работой.
