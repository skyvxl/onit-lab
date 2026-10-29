# Гостевая книга — ОНИТ, лабораторные 1 и 2

Приложение на Go хранит сообщения в PostgreSQL. Во второй работе Caddy принимает HTTP-запросы и распределяет их между тремя репликами приложения. Gitea хранит код и образы, запускает тесты и выкладывает новую версию.

Нужны Docker с Compose, Python 3, Git и Make. Для запуска Go вне контейнера нужна версия из `go.mod`.

## Первый локальный запуск

```sh
make init
make up
make smoke
```

Открыть [http://localhost](http://localhost). Если порт 80 занят, указать `HTTP_PORT` в `.env` и передавать адрес проверкам: `python3 scripts/check-http.py smoke --url http://localhost:8080`.

`make init` сохраняет существующий `.env`; для нового файла генерирует пароль БД. `make up` предназначен для первого запуска разработки: он собирает `guestbook:local`. После выкладки релиза обновлять приложение через `deploy.sh` или CI — повторный `make up` может заменить релиз локальным образом.

## Gitea и первая отправка кода

```sh
make gitea
git status
git add .
git diff --cached --stat
git commit -m "feat: complete ONIT lab 2"
git remote add gitea http://localhost:3000/student/guestbook.git
git push -u gitea main
```

Перед коммитом просмотреть подготовленные изменения. Если remote `gitea` уже есть, пропустить `git remote add`; существующий `origin` менять не нужно. При запросе авторизации использовать пользователя `student` и пароль из локального `infra/.credentials.json`.

`make gitea` создаёт пользователя, репозиторий, secrets и runner. Пароль и токен сохраняются в `infra/.credentials.json` с правами `600`; файл исключён из Git. Не вставлять его содержимое в отчёт или логи. Интерфейс Gitea: [http://localhost:3000/student/guestbook](http://localhost:3000/student/guestbook).

В pull request выполняются тесты. Push в `main` дополнительно собирает образы с тегом commit SHA, публикует их и запускает деплой. Ручной workflow `Rollback` принимает тег предыдущего образа.

Сценарии проверки, работа с registry и объяснение деплоя — в [docs/lab2.md](docs/lab2.md). Результаты выполненных опытов — в [docs/verification.md](docs/verification.md).

Исходное состояние первой работы сохранено тегом `laba1`. Текущий Compose использует проект `guestbook` и отдельный том БД; данные старого проекта автоматически не переносятся. `make down` останавливает стенд без удаления томов; `down -v` для этой работы не нужен.
