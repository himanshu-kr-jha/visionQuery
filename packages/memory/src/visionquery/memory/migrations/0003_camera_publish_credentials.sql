-- What a pushed camera publishes with (visionquery.memory.cameras.PublishCredentials): MediaMTX
-- lets a camera publish to its path only with these, and nobody publishes to a pushed camera
-- that has none.
--
-- The password is kept only as an argon2id hash (its PHC string), which is all MediaMTX needs
-- to check it (`argon2:` + the hash); removing the camera forgets both.
ALTER TABLE cameras
    ADD COLUMN publish_user          text CHECK (publish_user ~ '^[A-Za-z0-9._-]{1,64}$'
                                                 AND lower(publish_user) <> 'any'),
    ADD COLUMN publish_password_hash text CHECK (publish_password_hash LIKE '$argon2id$%'),
    ADD CONSTRAINT cameras_publish_credentials_are_whole
        CHECK ((publish_user IS NULL) = (publish_password_hash IS NULL)),
    ADD CONSTRAINT cameras_publish_credentials_are_a_pushed_cameras
        CHECK (publish_user IS NULL OR push_path IS NOT NULL),
    ADD CONSTRAINT cameras_removed_forgets_publish_credentials
        CHECK (removed_at IS NULL OR publish_user IS NULL);
