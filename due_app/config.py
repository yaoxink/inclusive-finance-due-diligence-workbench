"""Explicit due-app configuration. Never loads a dotenv or meeting settings."""
from dataclasses import dataclass
import os
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = APP_ROOT.parent
APP_ID = 'due-diligence'
VERSION = '0.5.0-workbench-synthetic-testing'


def checked_data_root(value):
    root = Path(value).expanduser().resolve()
    if root == Path(root.anchor):
        raise ValueError('不能将磁盘根目录用作尽调数据目录。')
    protected = [WORKSPACE / 'meeting-demo' / 'data', WORKSPACE / 'meeting-demo' / 'data-benchmark',
                 WORKSPACE / '材料', WORKSPACE / 'planning']
    if any(root == directory.resolve() or directory.resolve() in root.parents for directory in protected):
        raise ValueError('尽调数据必须与会议、语音评测和材料目录隔离。')
    if root.exists() and not root.is_dir():
        raise ValueError('尽调数据根不是目录。')
    marker = root / '.due-app-data-root'
    if marker.exists() and marker.read_text(encoding='utf-8').strip() != APP_ID:
        raise ValueError('数据目录属于其他应用。')
    if root.exists() and not marker.exists() and any(root.iterdir()):
        raise ValueError('请选择空的独立数据目录，不能接管已有资料目录。')
    return root


@dataclass(frozen=True)
class Settings:
    data_root: Path
    instance_id: str

    @classmethod
    def create(cls, data_root, instance_id):
        return cls(checked_data_root(data_root or os.environ.get('DUE_DATA_DIR') or APP_ROOT / 'data'), instance_id)

    def public_policy(self):
        return {'app_id': APP_ID, 'local_only': True, 'data_class': 'synthetic',
                'external_services_enabled': False, 'config_source': 'due_app_only', 'data_namespace': APP_ID}

    @property
    def asr_home(self):
        return Path(os.environ.get('DUE_ASR_HOME', 'E:/tools/audio-transcription/FunASR')).resolve()

    @property
    def capture_python(self):
        relative = 'Scripts/python.exe' if os.name == 'nt' else 'bin/python'
        return Path(os.environ.get('DUE_CAPTURE_PYTHON') or self.asr_home / '.venv' / relative).resolve()
