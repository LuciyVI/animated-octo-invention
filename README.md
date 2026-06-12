# Telegram bot for existing 3x-ui

Бот управляет клиентскими конфигурациями в уже запущенной панели 3x-ui. В целевой модели он проверяет членство пользователя в разрешённой Telegram-группе, автоматически создаёт для него отдельный inbound из template `Moroz`, привязывает inbound к Telegram ID и даёт создать до 5 устройств внутри собственного inbound. По умолчанию бот не удаляет inbound и не меняет core-настройки template или пользовательских inbound: port/protocol/streamSettings/sniffing/TLS/REALITY не редактируются после создания. Core-редактирование собственного inbound можно включить отдельным флагом `USER_CAN_CHANGE_INBOUND_CORE_SETTINGS=true`. Основная модель:

```text
Telegram group member -> auto-created inbound_id -> up to 5 client configs
```

Telegram username не используется как ключ. Главный идентификатор клиента - числовой Telegram ID.

## Архитектура

- `bot/main.py` запускает aiogram 3 и фоновую задачу истечения доступа.
- `bot/db.py` хранит SQLite-таблицы `users`, `configs`, `audit_log`, `port_allocations`, `provisioning_locks`.
- `bot/xui_client.py` подключается к существующему 3x-ui через API token.
- `bot/auto_provision.py` создаёт пользовательский inbound из template `Moroz` после проверки Telegram-группы.
- `bot/services.py` содержит бизнес-логику привязки, лимитов, отключения и sync.
- `bot/link_builder.py` строит share links из существующих параметров inbound.
- `bot/handlers/` содержит пользовательские и админские команды.

## Подключение к уже запущенному 3x-ui

В `.env` укажите URL существующей панели и API token:

```env
XUI_HOST=http://127.0.0.1:54321/
XUI_TOKEN=your-api-token
PUBLIC_HOST=vpn.example.com
```

`XUI_HOST` должен указывать на уже работающий 3x-ui. Docker Compose из этого проекта запускает только Telegram-бота и не запускает 3x-ui.

## 3x-ui 3.3.0 API setup

3x-ui 3.3.0 включает встроенную API-документацию и Bearer token auth.

- API docs: откройте Swagger UI в панели.
- OpenAPI JSON: `<XUI_HOST>/panel/api/openapi.json`.
- API token: `Settings -> Security -> API Token`.
- Бот не использует username/password login по умолчанию.
- Бот отправляет token только как `Authorization: Bearer <token>`.
- Read endpoints: `GET /panel/api/inbounds/list`, `GET /panel/api/inbounds/get/{id}`.
- 3x-ui 3.3.0 client mutate endpoints: `POST /panel/api/clients/add`, `POST /panel/api/clients/update/{email}`.
- Inbound create endpoint for explicit admin template clone: `POST /panel/api/inbounds/add`.
- Inbound update endpoint for explicitly enabled own-inbound JSON editing: `POST /panel/api/inbounds/update/{id}`.
- Legacy fallback for older panels remains available: `POST /panel/api/inbounds/addClient`, `POST /panel/api/inbounds/updateClient/{uuid}`.

Примеры `XUI_HOST`:

```env
# root path "/"
XUI_HOST=http://127.0.0.1:54321/

# root path "/secret-path/"
XUI_HOST=http://127.0.0.1:54321/secret-path/
```

Проверка из shell:

```bash
python scripts/smoke_real_xui_readonly.py
```

Проверка в Telegram:

```text
/api_check
```

Типовые ошибки:

- `401/403`: неправильный `XUI_TOKEN` или token удалён в панели.
- `404`: неверный root path в `XUI_HOST`.
- `connection refused`: неверный host/port или панель не слушает этот адрес.
- `empty inbounds`: token смотрит не в ту панель, нет доступа или inbounds действительно отсутствуют.

## Настройка `.env`

Скопируйте пример:

```bash
cp .env.example .env
```

Заполните:

```env
BOT_TOKEN=telegram-bot-token
ADMIN_IDS=111111111,222222222
XUI_HOST=http://127.0.0.1:54321/
XUI_TOKEN=3x-ui-api-token
PUBLIC_HOST=vpn.example.com
REQUIRE_GROUP_MEMBERSHIP=true
ACCESS_GROUP_ID=-1001234567890
AUTO_PROVISION_INBOUND=true
TEMPLATE_INBOUND_REMARK=Moroz
TEMPLATE_INBOUND_ID=
PORT_MIN=30000
PORT_MAX=39999
MAX_CONFIGS_PER_INBOUND=5
DEFAULT_ACCESS_DAYS=30
DEFAULT_CLIENT_TRAFFIC_GB=0
USER_CAN_MANAGE_OWN_INBOUND=true
USER_CAN_DISABLE_OWN_CLIENTS=true
USER_CAN_DISABLE_OWN_INBOUND=false
USER_CAN_CHANGE_INBOUND_CORE_SETTINGS=false
SHOW_TECHNICAL_IDS_TO_USERS=false
DB_PATH=./bot.db
```

## Automatic inbound provisioning by Telegram group membership

1. В 3x-ui создайте рабочий template inbound с exact `remark`:

```text
Moroz
```

2. Убедитесь, что inbound `Moroz` работает и поддерживается link builder:

```text
/check_template
```

3. Добавьте бота в приватную Telegram-группу.
4. В группе выполните:

```text
/group_id
```

5. Заполните `.env`:

```env
REQUIRE_GROUP_MEMBERSHIP=true
ACCESS_GROUP_ID=-100...
AUTO_PROVISION_INBOUND=true
TEMPLATE_INBOUND_REMARK=Moroz
PORT_MIN=30000
PORT_MAX=39999
MAX_CONFIGS_PER_INBOUND=5
```

6. Если inbound-порты должны быть доступны снаружи, откройте `PORT_MIN..PORT_MAX` в firewall или пробросьте range в Docker. Сам бот firewall не меняет.
7. Пользователь открывает бота в private chat и нажимает `/start`.
8. Бот проверяет `getChatMember` для `ACCESS_GROUP_ID`.
9. Если пользователь состоит в группе и ещё не имеет inbound, бот:
   - находит template: сначала `TEMPLATE_INBOUND_ID`, иначе exact `remark == TEMPLATE_INBOUND_REMARK`;
   - выбирает свободный port из `PORT_MIN..PORT_MAX`;
   - клонирует template через `POST /panel/api/inbounds/add`;
   - очищает `settings.clients`;
   - задаёт `remark=tg_<telegram_id>`, `tag=tg_<telegram_id>`, `enable=true`;
   - проверяет, что protocol/streamSettings/sniffing/listen совпали с template;
   - привязывает inbound к Telegram ID в SQLite.
10. Пользователь создаёт устройства кнопками. Каждый Telegram ID получает максимум 1 inbound, в каждом inbound максимум 5 active clients.

Template `Moroz` нельзя привязывать к пользователю и нельзя использовать как пользовательский inbound. Clients из `Moroz` не копируются.

Предупреждение: если Telegram-группа публичная, любой её участник сможет получить inbound. Используйте приватную группу или дополнительный approve/payment layer.

## Команды пользователя

- `/start` проверяет group membership, при необходимости автоматически создаёт inbound и показывает меню.
- `/menu` показывает плиточное меню кнопками.
- `/myid` показывает числовой Telegram ID без обращения к 3x-ui.
- `/my_access` показывает, разрешён ли доступ по Telegram-группе.
- `/create_access` в новой модели повторно запускает проверку доступа/auto-provision.
- `/status` показывает статус, активные устройства, лимит и срок доступа.
- `/configs` показывает только конфигурации текущего Telegram ID.
- `/new_config [title]` создаёт новый client/UUID внутри уже привязанного inbound.
- `/delete_config <number>` отключает выбранную конфигурацию.

Команды, которые показывают конфиги, работают только в private-чате с ботом.

## Меню кнопками

После `/start` или `/menu` бот показывает нижнюю клавиатуру-плитки.

Пользовательские плитки:

- `Статус доступа`
- `Мои устройства`
- `Добавить устройство`
- `Настройки доступа`
- `Настроить inbound`
- `Мой Telegram ID`
- `Мой доступ`
- `Инструкция`
- `Меню`

Админы дополнительно видят:

- `API check`
- `Список inbound`
- `Проверить inbound`
- `Создать inbound`
- `Привязать`
- `Sync`
- `Проверить шаблон Moroz`
- `Статус автосоздания`
- `Список пользователей`
- `Найти пользователя`

Плитки с обязательными параметрами не выполняют опасное действие без аргументов. Например `Создать inbound` показывает формат `/create_inbound...`, а не создаёт inbound сразу. Плитка `Добавить устройство` сначала показывает выбор типа устройства и подтверждение; слот конфигурации расходуется только после финального подтверждения или ручной fallback-команды `/new_config phone`.

Плитка `Инструкция` отправляет подробный порядок: как администратору подготовить template inbound `Moroz`, как пользователю настроить собственный inbound через guarded JSON-flow и как выпустить конфигурацию устройства.

## Настройка собственного inbound

Плитка `Настроить inbound` показывает inbound, привязанный к текущему Telegram ID: remark, protocol, port, enable и число clients. По умолчанию это read-only экран.

Чтобы разрешить пользователю менять core-поля своего inbound, администратор должен явно включить:

```env
USER_CAN_CHANGE_INBOUND_CORE_SETTINGS=true
```

После этого в экране `Настроить inbound` появится кнопка `Редактировать JSON`.

Flow:

1. Пользователь открывает `Настроить inbound`.
2. Нажимает `Редактировать JSON`.
3. Отправляет JSON object с полями, которые нужно изменить.
4. Бот показывает подтверждение.
5. Только после подтверждения бот вызывает `POST /panel/api/inbounds/update/{id}`.

Доступные поля JSON: `enable`, `remark`, `listen`, `port`, `protocol`, `expiryTime`, `total`, `settings`, `streamSettings`, `sniffing`, `tag`.

Ограничения:

- `id` игнорируется.
- `settings.clients` всегда сохраняется текущим и не берётся из пользовательского JSON, чтобы не стереть устройства.
- Template `Moroz` нельзя редактировать как пользовательский inbound.
- Бот не печатает секретные значения и не логирует полный JSON payload.
- Изменение `port`, `protocol`, `streamSettings`, TLS/REALITY или `sniffing` может сломать подключение. Перед включением сделайте backup базы 3x-ui.

## Команды администратора

- `/list_inbounds` читает список существующих inbound из 3x-ui.
- `/api_check` проверяет OpenAPI, Bearer auth и required endpoints.
- `/check_template` проверяет template inbound `Moroz` и поддержку auto-provision.
- `/auto_status` показывает диапазон портов, занятые/свободные ports и число auto users.
- `/users` показывает краткий список пользователей.
- `/access_check <telegram_id>` проверяет доступ пользователя по Telegram-группе.
- `/inbound <inbound_id>` показывает детали inbound и поддержку генерации ссылки.
- `/check_inbound <inbound_id>` показывает read-only health конкретного inbound.
- `/test_link <inbound_id>` проверяет, поддержит ли `link_builder` этот inbound, без создания client.
- `/bind <telegram_id> <inbound_id> <days>` связывает Telegram ID с уже существующим inbound.
- `/bind_dry_run <telegram_id> <inbound_id> <days>` проверяет bind без изменений в 3x-ui и SQLite.
- `/create_inbound_dry_run <template_inbound_id> <telegram_id> <days> <port|auto> <remark>` проверяет создание inbound из template без изменений.
- `/create_inbound <template_inbound_id> <telegram_id> <days> <port|auto> <remark>` создаёт inbound из template и сразу привязывает Telegram ID.
- `/new_config_dry_run <telegram_id> <title>` проверяет создание config без изменений в 3x-ui и SQLite.
- `/unbind <telegram_id>` ставит пользователю `status='unbound'`.
- `/disable <telegram_id>` отключает пользователя и его clients, но не inbound.
- `/extend <telegram_id> <days>` продлевает доступ.
- `/user <telegram_id>` показывает локальные configs и число active clients в inbound.
- `/revoke_config <telegram_id> <number>` отключает конкретную конфигурацию.
- `/sync` сверяет локальные inbound_id с 3x-ui и помечает отсутствующие как `orphaned`.

## Привязка Telegram ID к inbound

1. Админ смотрит существующие inbound:

```text
/list_inbounds
```

2. Клиент узнаёт свой ID:

```text
/myid
```

3. Админ привязывает клиента:

```text
/bind 123456789 7 30
```

`/bind` не создаёт inbound, не меняет inbound и только пишет связь в локальную SQLite БД.

## Создание inbound из template

Новый inbound можно создать только явной командой из существующего template inbound:

- админской командой `/create_inbound`;
- self-service командой `/create_access`, если это явно включено в `.env`.

Бот:

1. читает template inbound через `GET /panel/api/inbounds/get/{id}`;
2. проверяет, что Telegram ID ещё не привязан;
3. проверяет, что новый port свободен среди видимых inbound;
4. копирует `protocol`, `settings`, `streamSettings`, `sniffing`, `listen`, `enable`, `expiryTime`, `total`;
5. очищает `settings.clients`;
6. меняет только `remark` и `port`;
7. создаёт inbound через `POST /panel/api/inbounds/add`;
8. перечитывает созданный inbound;
9. проверяет, что clients пустые и template-поля совпали;
10. привязывает Telegram ID к созданному inbound в SQLite.

Dry-run:

```text
/create_inbound_dry_run <template_inbound_id> <telegram_id> <days> <port|auto> <remark>
```

Реальное создание:

```text
/create_inbound <template_inbound_id> <telegram_id> <days> <port|auto> <remark>
```

Пример:

```text
/create_inbound_dry_run 14 1452759621 30 auto client-1452759621
/create_inbound 14 1452759621 30 auto client-1452759621
```

Если указать `auto`, бот выберет свободный port, которого нет в `/list_inbounds`. Если 3x-ui работает в Docker без host network, убедитесь, что выбранный port доступен снаружи контейнера.

### Self-service `/create_access`

По умолчанию self-service выключен. Если пользователь выполнит `/create_access`, бот ответит, что доступ должен создать администратор командой `/create_inbound`.

Чтобы включить self-service, задайте template inbound:

```env
SELF_SERVICE_CREATE_ACCESS=true
SELF_SERVICE_TEMPLATE_INBOUND_ID=14
```

После перезапуска `/create_access`:

1. проверит, что Telegram ID ещё не привязан;
2. клонирует `SELF_SERVICE_TEMPLATE_INBOUND_ID`;
3. очистит clients в новом inbound;
4. выберет свободный port автоматически;
5. задаст `remark=tg_<telegram_id>`;
6. привяжет Telegram ID к новому inbound;
7. предложит создать config через `/new_config phone`.

Если включён group access gate, `/create_access` доступен только участникам разрешённой группы. Если group access gate выключен, self-service фактически становится публичной регистрацией, поэтому держите `SELF_SERVICE_CREATE_ACCESS=false` либо используйте приватную Telegram-группу.

## Создание конфигов клиентом

Клиент выполняет:

```text
/new_config phone
```

Бот:

1. берёт Telegram ID из Telegram API;
2. проверяет пользователя в SQLite;
3. читает уже существующий inbound из 3x-ui;
4. проверяет лимиты по локальной БД и фактическим active clients inbound;
5. сохраняет snapshot immutable fields inbound;
6. добавляет только client в существующий inbound;
7. повторно читает inbound и сравнивает immutable fields;
8. строит share link из параметров inbound;
9. отправляет ссылку и QR-код.

Защищённые поля inbound: `id`, `port`, `protocol`, `remark`, `enable`, `streamSettings`, `sniffing`, `listen`, `tag`. Если после add client эти поля изменились, бот пишет `critical_inbound_immutable_changed` в `audit_log` и отправляет предупреждение администраторам.

## Лимит 5 конфигураций

Лимит применяется на inbound. Учитываются:

- active configs в локальной SQLite БД;
- active clients, реально существующие в inbound 3x-ui;
- clients, созданные не ботом.

Если inbound уже содержит 5 active clients, `/new_config` откажет. Внутри процесса бота создание конфигураций защищено per-inbound async lock, чтобы две быстрые команды не создали шестую конфигурацию.

## Отключение клиента

Пользователь:

```text
/delete_config 1
```

Админ:

```text
/revoke_config 123456789 1
/disable 123456789
```

Бот отключает client в 3x-ui и ставит `enabled = 0` в SQLite. Записи из БД и inbound физически не удаляются.

## Sync

Команда:

```text
/sync
```

Бот читает все inbound из 3x-ui и сверяет с локальными `users.inbound_id`. Если inbound исчез, пользователь получает `status='orphaned'`. Sync ничего не создаёт и не удаляет в 3x-ui.

## Telegram group access control

Группа может работать как access whitelist. Если `REQUIRE_GROUP_MEMBERSHIP=true`, пользовательские команды в private-чате разрешены только участникам указанной Telegram-группы. Админы из `ADMIN_IDS` по умолчанию имеют доступ независимо от членства в группе.

Настройка:

1. Добавьте бота в нужную Telegram-группу.
2. В группе выполните:

```text
/group_id
```

3. Скопируйте `Group ID` в `.env`:

```env
REQUIRE_GROUP_MEMBERSHIP=true
ACCESS_GROUP_ID=-1001234567890
GROUP_MEMBERSHIP_CACHE_TTL_SEC=300
DISABLE_ACCESS_WHEN_LEFT_GROUP=false
```

4. Перезапустите бота:

```bash
systemctl --user restart telegram-3xui-bot.user.service
```

5. Проверьте от админа:

```text
/access_check <telegram_id>
```

6. Проверьте от клиента:

```text
/my_access
/create_access
```

Разрешённые Telegram membership statuses: `creator`, `administrator`, `member`, а также `restricted` при `is_member=true`. Запрещённые: `left`, `kicked`, `restricted` при `is_member=false`.

Бот не получает список всех участников группы. Он проверяет только конкретного пользователя через Telegram Bot API `get_chat_member` и кеширует результат на `GROUP_MEMBERSHIP_CACHE_TTL_SEC` секунд.

Предупреждения:

- Группа является whitelist. Если пользователь не состоит в группе, бот откажет.
- Ссылки и QR отправляются только в private chat.
- Если включён self-service без approval, любой участник группы сможет создать доступ.
- Если группа публичная, это фактически публичная регистрация. Лучше использовать приватную группу.
- `DISABLE_ACCESS_WHEN_LEFT_GROUP=false` только запрещает новые команды после выхода из группы. Если поставить `true`, бот при следующей проверке отключит локального пользователя и его clients.

## Safe production checklist

1. Сделайте backup базы 3x-ui. Обычно это SQLite-файл самой панели, и он критичен, потому что бот меняет список `clients` внутри inbound.
2. Проверьте read-only подключение:

```bash
python scripts/smoke_real_xui_readonly.py
```

3. В Telegram админом выполните:

```text
/list_inbounds
/check_inbound <inbound_id>
/test_link <inbound_id>
```

4. Проверьте привязку без изменений:

```text
/bind_dry_run <telegram_id> <existing_inbound_id> 30
```

5. Сделайте реальную привязку:

```text
/bind <telegram_id> <existing_inbound_id> 30
```

6. Проверьте создание config без изменений:

```text
/new_config_dry_run <telegram_id> phone
```

7. Клиентом выполните:

```text
/new_config phone
/configs
```

8. Сравните inbound до/после через `/check_inbound <inbound_id>` и панель 3x-ui.
9. Проверьте config на устройстве.
10. Проверьте revoke/disable:

```text
/revoke_config <telegram_id> 1
/disable <telegram_id>
```

Для optional modify smoke используйте только на тестовом inbound или после backup:

```bash
python scripts/smoke_real_xui_add_client.py <inbound_id> --i-understand-this-will-modify-3x-ui
```

## Docker Compose

```bash
docker compose up -d --build
```

Compose запускает только Telegram-бота, читает `.env`, подключается к внешнему 3x-ui по `XUI_HOST` и хранит SQLite в volume `bot-data`.

## Systemd

Пример unit находится в `systemd/telegram-3xui-bot.service`.

Установка:

```bash
python3.11 -m venv /opt/telegram-3xui-bot/venv
/opt/telegram-3xui-bot/venv/bin/pip install -r /opt/telegram-3xui-bot/requirements.txt
sudo cp systemd/telegram-3xui-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now telegram-3xui-bot
```

## Локальный запуск

```bash
python3.11 -m venv venv
venv/bin/pip install -r requirements.txt
venv/bin/python -m bot.main
```

## Тесты

```bash
pytest
python -m compileall bot tests scripts
python scripts/smoke_real_xui_readonly.py
```

Тесты используют fake XUIService и не обращаются к реальному 3x-ui.

## Troubleshooting

- `/myid` не работает: проверьте `BOT_TOKEN` и что бот запущен.
- `/list_inbounds` пустой: проверьте `XUI_HOST`, `XUI_TOKEN` и доступность API 3x-ui.
- `smoke_real_xui_readonly.py` пишет, что `httpx` не установлен: запустите скрипт через venv (`.venv/bin/python scripts/smoke_real_xui_readonly.py`) или установите зависимости в текущий Python (`python3 -m pip install -r requirements.txt`).
- `/bind` пишет `Inbound не найден`: inbound_id должен уже существовать в панели.
- `/new_config` пишет про лимит: проверьте active clients в inbound, включая clients, созданные не ботом.
- `cannot unmarshal string into Go struct field Client.client.tgId of type int64`: 3x-ui 3.3.0 ждёт `tgId` как integer в `POST /panel/api/clients/add`. Обновите бота до версии, где `tgId` отправляется числом, затем проверьте:
  ```bash
  .venv/bin/python -m pytest
  .venv/bin/python -m compileall bot tests scripts
  python3 scripts/smoke_real_xui_readonly.py
  sqlite3 bot.db "PRAGMA foreign_key_list(configs);"
  ```
  В выводе SQLite для `configs` должно быть `users|tg_id|tg_id`.
- Link generation не поддержана: минимально реализованы VLESS REALITY и VLESS TLS/WS. Для VMess/Trojan/Shadowsocks/Hysteria2 бот явно откажет в генерации ссылки.
- Не видите новые configs: команда `/configs` показывает только записи текущего Telegram ID.
- Проверка, что inbound не менялся: до `/bind` и после `/bind` сравните вывод `/inbound <id>` в 3x-ui. `/bind` использует только read-only вызовы к панели и запись в SQLite.
