CREATE TABLE accounts (
            handle TEXT PRIMARY KEY,
            display_name TEXT,
            tiktok_user_id TEXT,
            sec_uid TEXT,
            kind TEXT NOT NULL CHECK(kind IN ('ugc', 'brand')),
            enabled INTEGER NOT NULL DEFAULT 1,
            first_seen_at TEXT,
            last_seen_at TEXT
        );

CREATE TABLE ad_metric_snapshots (
            usage_id INTEGER NOT NULL REFERENCES ad_usage(usage_id),
            captured_at TEXT NOT NULL,
            impressions INTEGER NOT NULL DEFAULT 0,
            reach INTEGER NOT NULL DEFAULT 0,
            clicks INTEGER NOT NULL DEFAULT 0,
            landing_page_views INTEGER NOT NULL DEFAULT 0,
            installs INTEGER NOT NULL DEFAULT 0,
            trials INTEGER NOT NULL DEFAULT 0,
            purchases INTEGER NOT NULL DEFAULT 0,
            spend REAL NOT NULL DEFAULT 0,
            revenue REAL NOT NULL DEFAULT 0,
            PRIMARY KEY(usage_id, captured_at)
        );

CREATE TABLE ad_usage (
            usage_id INTEGER PRIMARY KEY AUTOINCREMENT,
            video_id TEXT NOT NULL REFERENCES videos(video_id),
            platform TEXT NOT NULL,
            campaign_id TEXT,
            campaign_name TEXT,
            ad_set_id TEXT,
            ad_set_name TEXT,
            ad_id TEXT,
            ad_name TEXT,
            creative_variant TEXT NOT NULL DEFAULT 'original',
            angle_override TEXT,
            started_at TEXT,
            ended_at TEXT,
            status TEXT NOT NULL DEFAULT 'draft',
            notes TEXT,
            UNIQUE(platform, ad_id, creative_variant)
        );

CREATE TABLE catalog_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

CREATE TABLE comments (
            comment_id TEXT PRIMARY KEY,
            video_id TEXT NOT NULL REFERENCES videos(video_id),
            parent_comment_id TEXT,
            user_id TEXT,
            user_handle TEXT,
            user_nickname TEXT,
            text TEXT NOT NULL DEFAULT '',
            likes INTEGER NOT NULL DEFAULT 0,
            reply_count INTEGER NOT NULL DEFAULT 0,
            created_epoch INTEGER,
            is_author INTEGER NOT NULL DEFAULT 0,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL
        );

CREATE TABLE local_asset_ad_metric_snapshots (
            usage_id INTEGER NOT NULL REFERENCES local_asset_ad_usage(usage_id),
            captured_at TEXT NOT NULL,
            impressions INTEGER NOT NULL DEFAULT 0,
            reach INTEGER NOT NULL DEFAULT 0,
            clicks INTEGER NOT NULL DEFAULT 0,
            landing_page_views INTEGER NOT NULL DEFAULT 0,
            installs INTEGER NOT NULL DEFAULT 0,
            trials INTEGER NOT NULL DEFAULT 0,
            purchases INTEGER NOT NULL DEFAULT 0,
            spend REAL NOT NULL DEFAULT 0,
            revenue REAL NOT NULL DEFAULT 0,
            PRIMARY KEY(usage_id, captured_at)
        );

CREATE TABLE local_asset_ad_usage (
            usage_id INTEGER PRIMARY KEY AUTOINCREMENT,
            asset_id TEXT NOT NULL REFERENCES local_assets(asset_id),
            platform TEXT NOT NULL,
            campaign_id TEXT,
            campaign_name TEXT,
            ad_set_id TEXT,
            ad_set_name TEXT,
            ad_id TEXT,
            ad_name TEXT,
            creative_variant TEXT NOT NULL DEFAULT 'original',
            angle_override TEXT,
            started_at TEXT,
            ended_at TEXT,
            status TEXT NOT NULL DEFAULT 'draft',
            notes TEXT,
            UNIQUE(platform, ad_id, creative_variant)
        );

CREATE TABLE local_asset_relations (
            asset_id TEXT NOT NULL REFERENCES local_assets(asset_id),
            tiktok_video_id TEXT NOT NULL REFERENCES videos(video_id),
            relationship TEXT NOT NULL CHECK(relationship IN ('near_duplicate', 'shared_body_variant')),
            similarity REAL NOT NULL,
            frame_similarities_json TEXT NOT NULL,
            reviewed INTEGER NOT NULL DEFAULT 0,
            notes TEXT,
            PRIMARY KEY(asset_id, tiktok_video_id, relationship)
        );

CREATE TABLE local_asset_sources (
            source_path TEXT PRIMARY KEY,
            asset_id TEXT NOT NULL REFERENCES local_assets(asset_id),
            source_role TEXT NOT NULL DEFAULT 'original',
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL
        );

CREATE TABLE local_assets (
            asset_id TEXT PRIMARY KEY,
            sha256 TEXT NOT NULL UNIQUE,
            canonical_path TEXT NOT NULL,
            media_kind TEXT NOT NULL CHECK(media_kind IN ('video', 'image')),
            file_bytes INTEGER NOT NULL,
            duration_seconds REAL,
            width INTEGER,
            height INTEGER,
            video_codec TEXT,
            audio_codec TEXT,
            audio_path TEXT,
            transcript TEXT,
            transcript_source TEXT,
            analysis_json TEXT NOT NULL DEFAULT '{}',
            duplicate_tiktok_video_id TEXT REFERENCES videos(video_id),
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL
        , duplicate_method TEXT, duplicate_similarity REAL, visual_fingerprint_json TEXT);

CREATE TABLE sync_runs (
            sync_id TEXT PRIMARY KEY,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL,
            account_count INTEGER NOT NULL DEFAULT 0,
            video_count INTEGER NOT NULL DEFAULT 0,
            new_video_count INTEGER NOT NULL DEFAULT 0,
            downloaded_count INTEGER NOT NULL DEFAULT 0,
            failed_count INTEGER NOT NULL DEFAULT 0,
            monid_cost_usd REAL NOT NULL DEFAULT 0,
            notes TEXT
        );

CREATE TABLE video_metric_snapshots (
            video_id TEXT NOT NULL REFERENCES videos(video_id),
            captured_at TEXT NOT NULL,
            views INTEGER NOT NULL DEFAULT 0,
            likes INTEGER NOT NULL DEFAULT 0,
            platform_comments INTEGER NOT NULL DEFAULT 0,
            fetched_comments INTEGER NOT NULL DEFAULT 0,
            non_author_comments INTEGER NOT NULL DEFAULT 0,
            shares INTEGER NOT NULL DEFAULT 0,
            saves INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(video_id, captured_at)
        );

CREATE TABLE videos (
            video_id TEXT PRIMARY KEY,
            account_handle TEXT NOT NULL REFERENCES accounts(handle),
            author_id TEXT,
            source_url TEXT NOT NULL,
            caption TEXT NOT NULL DEFAULT '',
            created_epoch INTEGER,
            duration_seconds INTEGER,
            width INTEGER,
            height INTEGER,
            music_id TEXT,
            music_title TEXT,
            music_author TEXT,
            music_original INTEGER,
            hashtags_json TEXT NOT NULL DEFAULT '[]',
            video_path TEXT,
            audio_path TEXT,
            subtitle_path TEXT,
            thumbnail_path TEXT,
            raw_metadata_path TEXT,
            sha256 TEXT,
            file_bytes INTEGER,
            transcript TEXT,
            transcript_source TEXT,
            transcript_updated_at TEXT,
            analysis_json TEXT NOT NULL DEFAULT '{}',
            analysis_updated_at TEXT,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            is_available INTEGER NOT NULL DEFAULT 1,
            last_error TEXT
        , visual_fingerprint_json TEXT);
