"""Lossless source observations, typed metrics, and organic-to-paid lineage."""
import json
import re
import hashlib
from functools import lru_cache

from .util import secret_bytes

NOISE_TABLES=tuple('noise_'+name for name in ('snapshots','posts','observations','assets','media','media_attempts','platform_observations','hosted_objects','hosting_verifications','hosted_media','provider_documents'))
PROVIDER_TABLES=('records','source_files','source_schema','document_index')+NOISE_TABLES
TABLES=('source_events','source_records','source_entities','source_metrics','source_relationships','source_asset_links','source_imports','source_import_files')+PROVIDER_TABLES
COUNT_TABLES=('source_records','source_entities','source_metrics','source_relationships','source_asset_links','source_imports','source_import_files')+NOISE_TABLES
_json_screen_cache={}
_critical=frozenset({'apikey','apitoken','accesstoken','refreshtoken','clientsecret','secretaccesskey','privatekey','password','sessiontoken','cookie','cookies','ttchaintoken'})
_placeholder=re.compile(r'(?i)\s*(?:\[?redacted(?:[ _:\-](?:credential|jwt|cookie|auth|token|secret))?\]?|<[^>]+>|your[_ -].*|\$\{[^}]+\})\s*')

def placeholder(value):
    return isinstance(value,str) and (bool(_placeholder.fullmatch(value)) or not value.strip())

@lru_cache(maxsize=2048)
def field_name(key):
    return re.sub('[^a-z]','',key.lower())

SCHEMA='''
CREATE TABLE IF NOT EXISTS records(record_key TEXT PRIMARY KEY,source_path TEXT,json_pointer TEXT,provider TEXT,record_kind TEXT,snapshot TEXT,entity_id TEXT,post_id TEXT,campaign_id TEXT,adset_id TEXT,ad_id TEXT,platform TEXT,observed_at TEXT,date_start TEXT,date_stop TEXT,views REAL,likes REAL,comments REAL,shares REAL,spend REAL,impressions REAL,reach REAL,clicks REAL,payload_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS source_entities(
 entity_key TEXT PRIMARY KEY,kind TEXT NOT NULL,platform TEXT,source_id TEXT,
 account_key TEXT REFERENCES source_entities(entity_key) DEFERRABLE INITIALLY DEFERRED,
 parent_key TEXT REFERENCES source_entities(entity_key) DEFERRABLE INITIALLY DEFERRED,
 title TEXT,body TEXT,source_url TEXT,published_at TEXT,metadata_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS source_events(
 record_id TEXT PRIMARY KEY,kind TEXT NOT NULL,platform TEXT,entity_id TEXT,
 entity_key TEXT REFERENCES source_entities(entity_key) DEFERRABLE INITIALLY DEFERRED,
 source_path TEXT,source_sha256 TEXT,source_coordinate TEXT,captured_at TEXT,
 capture_context_json TEXT NOT NULL,payload_json TEXT,
 payload_record_key TEXT REFERENCES records(record_key),raw_payload_sha256 TEXT NOT NULL,
 header_json TEXT NOT NULL,CHECK((payload_json IS NULL)!=(payload_record_key IS NULL)));
CREATE VIEW IF NOT EXISTS source_records AS
 SELECT e.record_id,e.kind,e.platform,e.entity_id,e.entity_key,e.source_path,
 e.source_sha256,e.source_coordinate,e.captured_at,e.capture_context_json,
 COALESCE(e.payload_json,r.payload_json) AS payload_json,e.raw_payload_sha256,e.header_json
 FROM source_events e LEFT JOIN records r ON r.record_key=e.payload_record_key;
CREATE TABLE IF NOT EXISTS source_metrics(
 key TEXT PRIMARY KEY,record_id TEXT REFERENCES source_events(record_id),
 entity_key TEXT REFERENCES source_entities(entity_key),captured_at TEXT,metric TEXT NOT NULL,
 value_json TEXT NOT NULL,value_number REAL,value_text TEXT,unit TEXT,source_coordinate TEXT,
 raw_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS source_relationships(
 key TEXT PRIMARY KEY,from_key TEXT NOT NULL REFERENCES source_entities(entity_key),
 to_key TEXT NOT NULL REFERENCES source_entities(entity_key),relationship TEXT NOT NULL,
 record_id TEXT REFERENCES source_events(record_id),raw_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS source_asset_links(
 key TEXT PRIMARY KEY,record_id TEXT REFERENCES source_events(record_id),
 entity_key TEXT REFERENCES source_entities(entity_key),sha256 TEXT REFERENCES vault_assets(sha256),
 role TEXT,ordinal INTEGER,raw_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS source_imports(
 bundle_id TEXT PRIMARY KEY,manifest_sha256 TEXT NOT NULL,report_json TEXT NOT NULL,
 manifest_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS source_import_files(
 bundle_id TEXT NOT NULL REFERENCES source_imports(bundle_id),source_path TEXT NOT NULL,
 stored_path TEXT NOT NULL,sha256 TEXT NOT NULL,source_sha256 TEXT,file_bytes INTEGER NOT NULL,
 kind TEXT NOT NULL,format TEXT,metadata_json TEXT NOT NULL,
 PRIMARY KEY(bundle_id,source_path));
CREATE INDEX IF NOT EXISTS source_kind_platform ON source_events(kind,platform,entity_id);
CREATE INDEX IF NOT EXISTS source_entity_account ON source_entities(account_key);
CREATE INDEX IF NOT EXISTS source_entity_parent ON source_entities(parent_key);
CREATE INDEX IF NOT EXISTS source_event_entity ON source_events(entity_key);
CREATE INDEX IF NOT EXISTS source_event_payload ON source_events(payload_record_key);
CREATE INDEX IF NOT EXISTS source_metric_query ON source_metrics(metric,entity_key,captured_at);
CREATE INDEX IF NOT EXISTS source_metric_record ON source_metrics(record_id);
CREATE INDEX IF NOT EXISTS source_metric_entity ON source_metrics(entity_key);
CREATE INDEX IF NOT EXISTS source_lineage_from ON source_relationships(from_key,relationship);
CREATE INDEX IF NOT EXISTS source_lineage_to ON source_relationships(to_key,relationship);
CREATE INDEX IF NOT EXISTS source_lineage_record ON source_relationships(record_id);
CREATE INDEX IF NOT EXISTS source_asset_record ON source_asset_links(record_id);
CREATE INDEX IF NOT EXISTS source_asset_entity ON source_asset_links(entity_key);
CREATE INDEX IF NOT EXISTS source_asset_hash ON source_asset_links(sha256);
'''


def prepare(db):
    db.executescript(SCHEMA)


def available(db):
    return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")}


def credentials(value):
    """Reject credential values, while retaining schemas/prose/business IDs."""
    if isinstance(value,str):
        if placeholder(value):return False
        if secret_bytes(value.encode()):return True
        if value.lstrip().startswith(('{','[')):
            probe=value.lower()
            markers=('api_key','apikey','api-key','api_token','apitoken','access_token','accesstoken','refresh_token','refreshtoken','client_secret','clientsecret','secret_access','secretaccess','private_key','privatekey','password','session_token','sessiontoken','cookie','authorization')
            if any(marker in probe for marker in markers):
                key=hashlib.sha256(value.encode()).digest()
                if key in _json_screen_cache:return _json_screen_cache[key]
                try:
                    result=credentials(json.loads(value))
                    if len(_json_screen_cache)>=8192:_json_screen_cache.pop(next(iter(_json_screen_cache)))
                    _json_screen_cache[key]=result
                    return result
                except ValueError:pass
        return False
    if isinstance(value,list):return any(credentials(v) for v in value)
    if isinstance(value,dict):
        for key,v in value.items():
            name=field_name(str(key))
            schema=isinstance(v,dict) and bool(set(v)&{'type','properties','$ref','description','anyOf','oneOf'})
            if name in _critical and isinstance(v,(str,dict,list)) and v and not placeholder(v) and not schema:
                return True
            if name=='authorization' and isinstance(v,str) and re.match(r'(?i)^(?:bearer|basic)\s+\S{16,}$',v) and not placeholder(v):return True
            if credentials(v):return True
    return False
