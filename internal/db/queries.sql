-- name: GetMessages :many
SELECT *
FROM messages
ORDER BY id DESC;

-- name: CreateMessage :exec
INSERT INTO messages (
    text
)
VALUES (
    sqlc.arg('text')
);
