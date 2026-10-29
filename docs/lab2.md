# Лабораторная 2: запуск и проверка

Использованы Go и Caddy — замены Python и Nginx, согласованные для этой работы. Команды выполняются из корня репозитория. Ниже описаны сценарии проверки; их ожидаемый результат сам по себе не является протоколом выполненного опыта.

## Что где находится

`docker-compose.yaml` запускает Caddy, три `my_backend`, PostgreSQL и одноразовый Goose `migrate`. Образы приложения, мигратора и Caddy собираются отдельными Dockerfile в `docker/`. В приложении есть `/version`, `/health`, `/metrics`; страница показывает версию и имя реплики.

`caddy/Caddyfile` содержит балансировку, ограничение частоты запросов и access log. Caddy собирается с модулем `rate_limit`; конфигурация включена в образ. Рабочим контейнерам не нужны bind mounts конфигураций из каталога CI: путь временного checkout на runner не должен существовать на Docker host.

`.env` задаёт пароль БД и внешний `HTTP_PORT`. Старые `DB_HOST` и `DB_PORT` внутри Compose не используются: приложение обращается к `postgres:5432`. Имя проекта — `guestbook`; его том PostgreSQL отделён от томов первой работы. Состояние первой работы и прежний мониторинг доступны через тег `laba1`. Не удалять тома командой `down -v`.

## Первый запуск, версия и тесты

```sh
make init
make up
make test
curl -i http://localhost/version
curl -i http://localhost/health
python3 scripts/check-http.py smoke
```

`make up` — только первоначальный локальный запуск разработки. После публикации релиза применять `deploy.sh` или CI. Если `HTTP_PORT` отличается от 80, заменить адреса и передать `--url http://localhost:ПОРТ` скриптам проверок.

`/version` возвращает JSON с `version` и `host`. Те же значения приходят в `X-App-Version` и `X-Served-By`; Caddy дополнительно показывает адрес upstream в `X-Upstream`. Версия запекается в бинарник через `APP_VERSION` при сборке. Непустая переменная окружения `APP_VERSION` приложения имеет приоритет; при обычной локальной сборке значение — `dev`. Версия и hostname фиксируются при регистрации маршрутов.

`/health` проверяет соединение с БД с таймаутом 1 с: исправное приложение возвращает `{"status":"ok"}`, недоступная БД — HTTP 503. В Dockerfile есть отдельная стадия `test`; финальный Alpine-образ содержит бинарник и шаблон, без Go и тестов.

## Балансировка и масштабирование 3 → 5

Сначала выполнить этот опыт на локальном dev-стенде, до релизных деплоев:

```sh
python3 scripts/check-http.py smoke --replicas 3
docker compose up -d --no-deps --no-recreate --no-build --scale my_backend=5 my_backend
docker compose ps
# Дождаться healthy у всех пяти контейнеров.
python3 scripts/check-http.py smoke --replicas 5
LB_POLICY=least_conn docker compose up -d --no-deps --force-recreate caddy
python3 scripts/check-http.py smoke --replicas 5
LB_POLICY=round_robin docker compose up -d --no-deps --force-recreate caddy
docker compose up -d --no-deps --no-recreate --no-build --scale my_backend=3 my_backend
```

`round_robin` выбирает реплики по очереди; `least_conn` выбирает реплику с наименьшим числом активных запросов. На коротких последовательных запросах различие почти незаметно. Скрипт печатает распределение по hostname и сверяет JSON с заголовками.

Caddy запрашивает Docker DNS для `my_backend` через `dynamic a`, обновляя адреса каждые 2 с. Перезагрузка Caddy для изменения числа реплик не нужна. Но сам DNS не сообщает о готовности приложения: активных health checks у динамического пула нет. Поэтому обычный `compose --scale` не обеспечивает безопасную выкладку новой версии; для этого используется отдельный алгоритм деплоя ниже.

## Сети и отказ одной реплики

```sh
BACKEND=$(docker compose ps -q my_backend | head -n 1)
CADDY=$(docker compose ps -q caddy)
docker inspect "$BACKEND" --format '{{json .NetworkSettings.Networks}}'
docker inspect "$CADDY" --format '{{json .NetworkSettings.Networks}}'
docker network inspect guestbook_backend --format '{{.Internal}}'
docker exec "$BACKEND" sh -c 'nslookup postgres; nc -z -w 2 postgres 5432'
docker exec "$CADDY" sh -c 'nslookup my_backend; nc -z -w 2 postgres 5432'
```

Backend подключён к `frontend` и `backend`, Caddy — только к `frontend`, PostgreSQL — только к внутренней `backend`. Поэтому последняя проверка соединения Caddy с PostgreSQL должна завершиться ошибкой. Порт PostgreSQL наружу не опубликован. В Alpine используется `sh`, устанавливать Bash для этих команд не нужно.

В двух терминалах проверить перезапуск под запросами:

```sh
python3 scripts/check-http.py load --seconds 30
# Во втором терминале, после определения BACKEND:
docker exec "$BACKEND" sh -c 'kill -TERM 1'
docker inspect "$BACKEND" --format '{{.RestartCount}} {{.State.Health.Status}}'
docker compose logs --tail 30 my_backend
```

Go завершает текущую работу по SIGTERM, а `restart: unless-stopped` запускает процесс снова. Дождаться `healthy` и проверить увеличение `RestartCount`. В локальной среде `kill -9 1` из контейнера может игнорироваться из-за особенностей PID 1; здесь используется SIGTERM. Ошибки запросов во время отказа нужно записать в результаты опыта, а не считать отсутствие ошибок гарантией любой балансировки.

## Ограничение запросов и нагрузка

```sh
python3 scripts/check-http.py rate
python3 scripts/check-http.py load --seconds 30
```

По умолчанию разрешено 5 запросов в секунду с одного IP. `rate` отправляет всплеск и ожидает HTTP 429; обычный `load` отправляет 4 запроса/с. HTTP 429 означает срабатывание лимита, а HTTP 502 — проблему обращения к upstream.

Для отдельного опыта с высокой нагрузкой временно увеличить лимит и затем вернуть его:

```sh
RATE_LIMIT_EVENTS=10000 docker compose up -d --no-deps --force-recreate caddy
python3 scripts/check-http.py load --seconds 30 --rps 30
RATE_LIMIT_EVENTS=5 docker compose up -d --no-deps --force-recreate caddy
```

В access log Caddy видны IP и ответы. При сохранении сообщения Go пишет `client_ip` из `X-Real-IP`, который Caddy устанавливает из адреса клиента. Прямое обращение к приложению использует `RemoteAddr`, если заголовка нет.

## Gitea, registry и два образа

`make gitea` поднимает Gitea на `localhost:3000`, создаёт `student/guestbook`, runner и CI secrets. Пароль и PAT находятся в исключённом из Git `infra/.credentials.json` с правами `600`. Команды commit/push приведены в README; remote называется `gitea`, существующий `origin` сохраняется. Пароль нужен для входа в веб-интерфейс; PAT — для registry/API.

Пример ручной публикации двух версий. Токен читается в переменную, не выводится на экран; не включать `set -x`:

```sh
export REGISTRY_USER=student
export REGISTRY_TOKEN="$(python3 -c 'import json; print(json.load(open("infra/.credentials.json"))["token"])')"
export IMAGE=localhost:3000/student/guestbook
printf '%s' "$REGISTRY_TOKEN" | docker login localhost:3000 -u "$REGISTRY_USER" --password-stdin
for TAG in lab2-a lab2-b; do
  docker build --build-arg APP_VERSION="$TAG" -f docker/Dockerfile.main -t "$IMAGE:$TAG" .
  docker build -f docker/Dockerfile.migrate -t "$IMAGE-migrate:$TAG" .
  docker push "$IMAGE:$TAG"
  docker push "$IMAGE-migrate:$TAG"
done
```

Получить краткоживущий bearer token для Distribution API, посмотреть теги и digest:

```sh
export REGISTRY_BEARER="$(curl -fsS -u "$REGISTRY_USER:$REGISTRY_TOKEN" \
  'http://localhost:3000/v2/token?service=container_registry&scope=repository:student/guestbook:pull' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])')"
curl -fsS -H "Authorization: Bearer $REGISTRY_BEARER" http://localhost:3000/v2/student/guestbook/tags/list
curl -fsSI -H "Authorization: Bearer $REGISTRY_BEARER" \
  -H 'Accept: application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json' \
  http://localhost:3000/v2/student/guestbook/manifests/lab2-a
unset REGISTRY_TOKEN REGISTRY_BEARER
```

В ответе manifest интересует `Docker-Content-Digest`. Тег — имя, которое можно переназначить; digest идентифицирует содержимое. CI использует полный commit SHA как тег приложения и мигратора, а не изменяемый `latest`.

## Выкладка без остановки прежних реплик

```sh
python3 scripts/check-http.py load --seconds 60 > /tmp/lab2-load.txt &
LOAD_PID=$!
APP_IMAGE="$IMAGE:lab2-b" MIGRATE_IMAGE="$IMAGE-migrate:lab2-b" ./deploy.sh
wait "$LOAD_PID"
cat /tmp/lab2-load.txt
curl -s http://localhost/version
```

Обёртке `deploy.sh` нужен Python 3 на host или в CI job. Она переходит в корень проекта и запускает Python-оркестратор; стандартной библиотеки достаточно. `REPLICAS` по умолчанию равно 3, `DEPLOY_TIMEOUT` — 60 с.

Сначала создаётся глобальная блокировка `guestbook-deploy-lock` в Docker. Затем скрипт убеждается, что прежние контейнеры работают и healthy, сохраняет JSON конфигурации Caddy и закрепляет upstream за их IP. Goose выполняет миграции, Compose добавляет три контейнера без пересоздания прежних. До подтверждения `healthy` и нужного image ID новые контейнеры не получают трафик.

После проверки Caddy атомарно переключается на новый статический пул. Старые контейнеры останавливаются и удаляются по одному; после обновления DNS возвращается `dynamic a`. При первом запуске последовательность другая: PostgreSQL → healthy → миграции → приложение → healthy → Caddy.

Ошибка миграции останавливает выкладку до запуска новых реплик. Любая ошибка до переключения удаляет только новые контейнеры и восстанавливает прежний пул. Если ошибка произошла уже после переключения, новый пул продолжает обслуживать запросы, а скрипт сообщает о незавершённой очистке. Тома БД не удаляются.

Для сравнения на учебном стенде можно запустить тот же генератор нагрузки и обновить приложение обычным Compose:

```sh
python3 scripts/check-http.py load --seconds 30 > /tmp/lab2-naive.txt &
LOAD_PID=$!
APP_IMAGE="$IMAGE:lab2-a" docker compose up -d --no-deps --no-build my_backend
wait "$LOAD_PID"
cat /tmp/lab2-naive.txt
```

Обычное обновление пересоздаёт реплики без отдельного переключения готовых пулов. Возможны HTTP 502/503 или ошибки соединения. Количество зависит от времени запуска и частоты запросов; ноль ошибок в одном коротком опыте не доказывает отсутствие окна недоступности.

## Неудачная версия и откат

Создать образ, который запускается, но не открывает HTTP-порт, и проверить защиту деплоя:

```sh
printf 'FROM %s\nENTRYPOINT ["sh","-c","sleep 3600"]\n' "$IMAGE:lab2-b" \
  | docker build -t guestbook:broken -f - .
APP_IMAGE=guestbook:broken RUN_MIGRATIONS=0 DEPLOY_TIMEOUT=15 ./deploy.sh
curl -s http://localhost/version
```

Ошибка команды деплоя здесь ожидаема. Версия должна остаться прежней, а новые нездоровые контейнеры — исчезнуть. Для отката исправного релиза:

```sh
APP_IMAGE="$IMAGE:lab2-a" RUN_MIGRATIONS=0 ./deploy.sh
python3 scripts/check-http.py smoke
```

В Gitea тот же опыт выполняется через Actions → `Rollback` → тег предыдущего образа. Откатывается приложение; схема БД автоматически не понижается. `RUN_MIGRATIONS=0` пропускает миграции. Изменения схемы должны оставаться совместимыми с предыдущим приложением: сначала добавление новых полей, затем обновление кода, удаление старых полей — отдельным поздним релизом.

## CI и проверка остановки ошибочной сборки

`.gitea/workflows/ci.yml` выполняет Go-тесты в стадии `test` и Python-тесты. Pull request на этом заканчивается. Push в `main` собирает и публикует приложение с версией SHA и соответствующий мигратор; job `deploy` запускается только после успешного `build`. Доступы к registry и БД хранятся в Gitea secrets.

Runner 5.0.0 настроен с `container.sweep: false`: сервисы, созданные деплоем, остаются работать после завершения job. Без этой настройки runner удаляет их при уборке. Это поведение описано в [документации Gitea](https://docs.gitea.com/runner/jobs/). Runner имеет доступ к Docker host; запускать на нём только свой доверенный код.

Публикация и скачивание образов в workflows идут через `scripts/registry.py`. При временном обрыве (`EOF`, reset, timeout) он делает не более трёх попыток с паузами 2 и 4 с. Ошибки прав и отсутствующий образ не повторяются; неудачная последняя попытка возвращает ошибку и блокирует дальнейший деплой. Это обработка временных сбоев транспорта, а не доказательство их конкретной причины.

Чтобы продемонстрировать остановку ошибочной сборки, создать временную ветку, сломать утверждение теста `/health` или внести синтаксическую ошибку Go, отправить ветку и открыть PR. Ожидается красный job тестов, отсутствие публикации и деплоя; `/version` рабочего приложения остаётся прежним. Затем исправить ошибку и показать успешный повтор. Это сценарий опыта, а не утверждение, что он уже выполнен.

Для сквозной проверки автоматики используется отдельный локальный репозиторий `student/guestbook-verification`: так пробные CI-коммиты не меняют историю рабочего checkout пользователя. После проверки пользователь отправляет свои изменения в основной `student/guestbook` самостоятельно.

Trivy можно дополнительно подключить для сканирования образов. Это необязательное расширение; в текущем workflow оно не реализовано.
