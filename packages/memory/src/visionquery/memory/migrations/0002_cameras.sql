-- The camera registry (visionquery.memory.cameras): every camera of the Deployment, either
-- pulled by MediaMTX from its URL or publishing to a MediaMTX path of its own.
--
-- A pulled camera's URL is kept as its public part only (scheme, host and port) in pull_url.
-- Its secrets are kept apart: the user and password in pull_credentials and the path and query
-- in pull_target (both as written in the URL, percent-encoded; '' when the URL has no path or
-- query). What is shown of a camera never holds them, and removing the camera forgets them.
--
-- A removed camera keeps its row, because its Footage and Tracks outlive it and must still
-- say which camera saw them; its name and push path are free for a new camera.
CREATE TABLE cameras (
    id               integer     GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name             text        NOT NULL CHECK (name <> '' AND name = btrim(name)),
    pull_url         text        CHECK (pull_url ~ '^rtsps?://[^@/?#]+$'),
    pull_credentials text,
    pull_target      text,
    push_path        text        CHECK (push_path <> '' AND push_path !~ '^pulled/'),
    added_at         timestamptz NOT NULL DEFAULT now(),
    removed_at       timestamptz,
    CONSTRAINT cameras_pulled_or_pushed CHECK ((pull_url IS NULL) <> (push_path IS NULL)),
    CONSTRAINT cameras_credentials_are_a_pulled_cameras
        CHECK (pull_credentials IS NULL OR pull_url IS NOT NULL),
    CONSTRAINT cameras_target_is_a_pulled_cameras
        CHECK (pull_target IS NULL OR pull_url IS NOT NULL),
    CONSTRAINT cameras_pulled_not_removed_has_a_target
        CHECK (pull_url IS NULL OR removed_at IS NOT NULL OR pull_target IS NOT NULL),
    CONSTRAINT cameras_removed_forgets_secrets
        CHECK (removed_at IS NULL OR (pull_credentials IS NULL AND pull_target IS NULL))
);

-- Names are unique within the Deployment whatever their case, among cameras not removed.
-- Case is folded by Unicode's rules (pg_unicode_fast, built into Postgres), never by the
-- database's locale, in which lower() may fold only ASCII.
CREATE UNIQUE INDEX cameras_name_unique ON cameras (casefold(name COLLATE pg_unicode_fast))
    WHERE removed_at IS NULL;
-- MediaMTX path names are case-sensitive.
CREATE UNIQUE INDEX cameras_push_path_unique ON cameras (push_path) WHERE removed_at IS NULL;
