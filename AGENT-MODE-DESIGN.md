# v0.3 Agent Mode — проект, не реализация

25.09.2026. Следующий продуктовый этап после текущего LOCAL smoke/checkpoint и
расширения [Loop Replay](LOOP-REPLAY-PHASE2.md). Ничего из этого документа сейчас
не включает новые tools, системные службы или API. Правила — [AGENTS.md](AGENTS.md).

## Поток и доверие

Пользователь произносит просьбу → существующий видимый ChatGPT UI возвращает
JSON-план → локальный parser/validator → локальная политика подтверждения →
executor → проверка результата каждого шага → короткий голосовой итог.

GPT предлагает данные, не полномочия. Ответ страницы и содержимое заметки
недоверенны. Никаких shell, `eval`, Python, шаблонов команд, dynamic import,
инструкций из tool result или рекурсивного исполнения планов. Список tools
определяет приложение. Никаких API ChatGPT, ключей, cookies/session tokens,
скрытого состояния страницы. Извлечение — из видимого завершённого ответа
текущего запроса через уже существующий UI bridge, отдельной будущей задачей.

Принимать ровно один JSON object либо один fenced `json` block без окружающего
исполняемого содержания. Несколько планов, trailing data, duplicate JSON keys,
неизвестные поля, NaN/Infinity, превышение размера/depth — отказ до side effect.
Не «чинить» опасный JSON автоматически и не извлекать команды из обычного текста.
Ограничения первой версии: до 16 KiB, до 5 шагов, без вложенных планов/ветвлений.
Это проектные пределы безопасности, не измеренные performance targets.

## Registry и контракты

Каждый ToolSpec содержит имя/version, strict args, effect class, permission,
deadline, execute, verify, idempotency/unknown-outcome policy. Реализация локальная,
ни имени Python callable, ни пути к модулю в JSON. ToolSpec — источник политики,
а поле GPT `requires_confirmation` может только ужесточать её.

| Tool | Аргументы первой версии | Политика / проверка |
|---|---|---|
| `get_time`, `get_date`, `get_battery` | Пустой object | Read-only; тип результата, battery unavailable — понятный результат, не traceback |
| `set_volume` | `percent`: integer 0..100, bool не integer | Локальный обратимый effect; исполнение только по прямой просьбе, verify фактического уровня; при 0 отдельно предупредить о потере слышимости |
| `create_note` | `title`, `text`: строки с ограничением длины | Запись только в private notes root; один голосовой preview/approval до сохранения; новый ID, никаких перезаписей |
| `read_note` | `note_id`: валидный локальный ID | Чтение локально; не отправлять содержание в GPT без отдельного разрешения |
| `open_allowed_url` | `url`: ограниченная строка | Внешний effect, подтверждение точного назначения; только явно настроенные HTTPS origins |
| Reminders (позже) | Структурированное время/timezone, текст | Отдельный persistent scheduler/design; не запускать shell для таймера |

Удаление, сообщения, установка/запуск программ **не входят** в первую allowlist.
Если когда-либо добавлены отдельными tools, требуют подтверждения и собственных
тестов. Подтверждение не делает неизвестный tool разрешённым. Старую широкую
LOCAL команду закрытия Firefox нельзя автоматически экспортировать в registry.

## JSON plan v1 (предлагается)

```json
{
  "version": 1,
  "steps": [
    {
      "id": "s1",
      "tool": "set_volume",
      "args": {"percent": 40},
      "requires_confirmation": false,
      "check": {"kind": "volume_percent", "expected": 40}
    }
  ]
}
```

`check` — allowlisted декларативная проверка данного tool, не программа.
Executor всегда выполняет собственную обязательную verification, даже если
GPT предложил слабую проверку. Несовместимый check/args отклоняется. Первые планы
линейные, без подстановки произвольных строк из результатов и без путей из GPT.
Связанные действия с ID заметок добавить позже как типизированные ссылки,
не интерполяцию shell/URL.

Path sandbox: GPT видит только note ID. Локальный adapter открывает файлы внутри
выделенного каталога, запрещает traversal и symlinks, создаёт эксклюзивно/атомарно.
Одного `resolve()` перед поздним `open()` недостаточно: исключить подмену symlink
между проверкой и записью. Никакого доступа к профилю Firefox, домашним секретам
или произвольным абсолютным путям.

URL policy: парсинг, фиксированная allowlist origins, запрет credentials, file/data/
javascript, localhost/link-local/private IP и вложенных команд. Не обещать, что
allowlist первой ссылки контролирует все последующие browser redirects: для
первой версии разрешать известные точные назначения, не использовать этот tool
как загрузчик/сборщик данных. Будущую проверку redirects проектировать отдельно.

## Подтверждение и отмена

Без подтверждения: read-only local tools и явно запрошенные безвредные обратимые
действия, разрешённые локальной политикой. С подтверждением: внешние эффекты,
постоянные записи/изменения, чувствительные действия. GPT не может сам ответить
«да»: approval приходит только из следующего пользовательского voice context.

Подтверждение привязано к hash неизменяемого плана/шага, локальной session
generation, озвученным аргументам и конечному сроку. Изменение плана аннулирует
approval. Неопределённая речь/таймаут — не согласие. «Отмена» отменяет pending
шаги; старый ответ, approval или Future не оживляют новый план.
Существующие LOCAL/control grammar не менять в этом проходе; future confirmation
recognizer требует отдельного corpus/negative-test acceptance.

## Executor и recovery

Весь план валидируется до первого действия; перед каждым шагом повторно проверить
generation/cancel/preconditions. Read-only tool выполняется с deadline; mutable
tool должен иметь ограниченный механизм остановки, а не один `Future.timeout`
над бесконечно живущим worker. Классифицировать итог: verified success, known fail,
unknown. После timeout неизвестный side effect не повторять автоматически.

При partial failure остановить последующие шаги; озвучить, что выполнено, что
не выполнено и что неизвестно. Никакого автоматического «rollback» удалением
или другого destructive retry. Read-only retry допускается только конечной
политикой tool. Failed verification не превращается в success по сообщению GPT.
User cancel не гарантирует отмену уже принятого внешней системой действия;
такой результат явно unknown. Повторная попытка — новый план с новым approval.

## Память, журнал и интерфейсы

Сохранять только явно одобренные preferences/notes/config, с минимальными правами.
Не записывать весь разговор, каждый prompt, browser DOM и промежуточные планы.
Журнал: plan hash/step ID/tool/status/duration, безопасные error codes, без текста
заметок, cookies, tokens и локальных переменных traceback. Ответ tool остаётся
локальным; передача в UI GPT — отдельный явный выбор пользователя.

Предлагаемые будущие модули: `agent_plan.py` (parser/validator), `agent_tools.py`
(registry/adapters), `agent_executor.py` (шаги/verification/cancel),
`test_agent_plan.py`, `test_agent_executor.py`. Сначала режим «показать/озвучить
план без исполнения», затем только read-only tools, затем один reversible effect.
Интеграция в режимы — отдельное решение после tests, не новая функция сейчас.

Приёмка: malicious JSON/args/path/URL отклонены до эффекта; model flag не ослабляет
approval; cancel/late result/no-double-effect детерминированы; частичный провал
не запускает следующий tool; private content не попадает в logs/report; все tools
работают с fake adapters без сети, затем отдельный узкий live acceptance.
