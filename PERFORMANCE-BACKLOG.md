# Измеримый UX / performance backlog

25.09.2026. Это очередь измерений, не оптимизации текущего кода. Источники:
[ACCEPTANCE.md](ACCEPTANCE.md); logs остаются локальными. Численные targets,
которых нет в evidence, назначаются только после baseline и согласования UX.
P0 — текущий smoke gate, P1 — следующий reliability/release этап, P2 — после него.

| Приоритет | Симптом / свидетельство | Что измерять | Условие приёмки / выбор target |
|---|---|---|---|
| P0 | Immediate LOCAL: один final `михаила` → NONE, затем retry; новый FreshWake AUTO исправил stale partial | 5 wake/TIME, лишние wake, create/first Accept, субъективная пауза | Запрошенные 5/5, без второго wake, задержка приемлема пользователю; не выдавать это за статистику всех условий |
| P1 | Larisa startup: 77,9 с в live-final; другой запуск microphone failed на 99,9 с; новые mic ветки затем LIVE прошли | Раздельные stage elapsed, remaining budget, результат, причина отказа, ресурсная нагрузка | Сначала распределение на повторяемых стартах; не обещать <2 с и не расширять 240 с ради PASS |
| P1 | Speaker monitor stale в live-ready | Длительность недоступности, recovery attempts, время подтверждённой тишины, загрузка | Fail-closed всегда; baseline отделяет задержку чтения от dead process, без повышения freshness threshold |
| P1 | Audio stale/overflow под нагрузкой; старый wake queue дефект исправлен, в последнем LOCAL trace overflow=0 | Возраст/drop по причине, poll lag, queue depth, совпадение с synthesis/browser stages | Ни одна старая команда не исполняется; потерю начала речи воспроизвести до изменения policy |
| P1 | Firefox на 4 ГБ RAM; исторический Voice RSS ~1853 МиБ включает shared pages | Python/owned tree RSS, swap/zram, peak, stage latency; PSS только если доступно | Baseline в сопоставимых условиях, не убивать пользовательский Firefox ради цифр; целевой запас памяти выбрать после измерения |
| P1 | Atomic cancel во время медленного opening/mic wait LIVE прошёл | Финал ASR → close requested → reaped → LOCAL; разнести ASR/cleanup/TTS | Сохранить отсутствие Send retry и confirmed cleanup; задержку сравнивать со стадиями и существующим 12-с бюджетом, не со всей startup duration |
| P1 | FreshWake creation offline ~1 мс, native lifetime длительно не измерен | Count/rate creations, latency под нагрузкой, RSS тренд при повторных циклах | Не считать рост RSS сам по себе утечкой; проверить удержание и steady state, target после baseline |
| P2 | UX prompts/непонятное долгое opening | Повторные обращения, момент готовности, необходимость ручного ожидания | Пользователь понимает, когда говорить/отменять; менять prompts только после наблюдения, не добавлять ещё один cooldown |
| P2 | Длительная работа/обычный день ещё не приняты | Ошибки за время, незакрытые owned процессы, устойчивость ресурсов, recovery | Отдельный согласованный длительный прогон; отсутствие случайных действий/Send duplicates — обязательное, не средняя метрика |

Offline wake measurement уже выполнен, повторять сейчас не требуется. Его mean
0,986 мс относится к созданию recognizer с одной загруженной Model, не к задержке
микрофона/всего ответа. Текущий проход не выполняет benchmark или live.
