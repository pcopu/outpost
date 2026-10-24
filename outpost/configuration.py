"""Local nonsecret storage references; authentication stays with existing CLIs."""
import json
import os
from pathlib import Path
import uuid

from .storage import DirectoryStore,RcloneStore,WranglerStore
from .util import lock,secret_data,write_json


def validate(config):
    if not isinstance(config,dict) or config.get('format_version')!=1 or secret_data(config):
        raise ValueError('Invalid nonsecret storage reference')
    backend=config.get('backend')
    allowed={'format_version','backend','private_confirmed'}
    if backend=='directory':
        allowed|={'path'};DirectoryStore(config['path'])
    elif backend=='wrangler':
        allowed|={'bucket','account_id'}
        if not config.get('account_id'):
            raise ValueError('An explicit account ID is required for a default Wrangler reference')
        WranglerStore(config['bucket'],config['account_id'])
    elif backend=='rclone':
        allowed|={'remote'};RcloneStore(config['remote'])
    else:
        raise ValueError('Unknown storage backend')
    if set(config)-allowed or (backend!='directory' and config.get('private_confirmed') is not True):
        raise ValueError('Verify private bucket access before saving its reference')
    return config


def load(root):
    path=Path(root)/'storage.json'
    if path.is_symlink():
        raise ValueError('Symlink storage configuration')
    return validate(json.loads(path.read_text())) if path.exists() else None


def save(root,config,dry_run=False):
    config=validate(config);root=Path(root)
    previous=load(root)
    if previous:
        if previous!=config:
            raise ValueError('Storage is already configured differently; use explicit transfer options')
        return config
    if dry_run:
        return dict(config,dry_run=True)
    root.mkdir(parents=True,exist_ok=True,mode=0o700)
    with lock(root/'.registry.lock'):
        previous=load(root)
        if previous:
            if previous!=config:raise ValueError('Storage reference changed concurrently')
            return previous
        temp=root/('.storage-'+uuid.uuid4().hex)
        write_json(temp,config)
        try:
            os.link(temp,root/'storage.json',follow_symlinks=False)
        finally:
            temp.unlink()
    return config


def backend(config):
    validate(config)
    if config['backend']=='directory':return DirectoryStore(config['path'])
    if config['backend']=='rclone':return RcloneStore(config['remote'])
    return WranglerStore(config['bucket'],config['account_id'])
