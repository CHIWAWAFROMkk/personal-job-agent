import hashlib
from pathlib import Path
import shutil
import subprocess
import zipfile

import pytest
from scripts.package_source import build
from scripts.privacy_audit import SKIP_DIRS


def fixture_source(tmp_path: Path) -> Path:
    root = tmp_path / 'source'
    (root / 'scripts').mkdir(parents=True)
    shutil.copyfile(Path(__file__).resolve().parents[1] / 'scripts/privacy_audit.py', root / 'scripts/privacy_audit.py')
    (root / 'pyproject.toml').write_text('[project]\nversion = "1.0.0rc1"\n', encoding='utf-8')
    (root / 'src').mkdir()
    (root / 'src/example.py').write_text('value = 1\n', encoding='utf-8')
    return root


def test_package_has_dynamic_version_and_excludes_runtime_data(tmp_path):
    root = fixture_source(tmp_path)
    private = root / 'data/private'
    private.mkdir(parents=True)
    (private / 'profile.json').write_text('{"name":"synthetic private data"}', encoding='utf-8')
    (root / '.env').write_text('SYNTHETIC_KEY=private-only', encoding='utf-8')
    target = build(root)
    assert '1.0.0rc1' in target.name
    with zipfile.ZipFile(target) as archive:
        assert 'src/example.py' in archive.namelist()
        assert 'data/private/profile.json' not in archive.namelist()
        assert '.env' not in archive.namelist()
    assert target.with_suffix('.zip.sha256.txt').read_text().split()[0] == hashlib.sha256(target.read_bytes()).hexdigest()
    with pytest.raises(FileExistsError):
        build(root, target)


def test_accidental_private_file_in_source_blocks_archive(tmp_path):
    root = fixture_source(tmp_path)
    (root / 'src/accidental.sqlite3').write_bytes(b'synthetic not a real database')
    target = root / 'dist/blocked.zip'
    with pytest.raises(subprocess.CalledProcessError):
        build(root, target)
    assert not target.exists()


def test_every_audit_ignored_directory_is_excluded_from_delivery(tmp_path):
    root = fixture_source(tmp_path)
    for dirname in SKIP_DIRS | {'.IDEA', '.vscode', 'node_modules', 'nested.egg-info'}:
        hidden = root / 'docs' / dirname
        hidden.mkdir(parents=True, exist_ok=True)
        (hidden / 'credentials.env').write_text('SYNTHETIC_PASSWORD=private-only', encoding='utf-8')
    with zipfile.ZipFile(build(root)) as archive:
        assert not any(name.startswith('docs/') for name in archive.namelist())


@pytest.mark.parametrize('filename', ['.env', '.env.local', '.ENV.production', 'profile.json', 'app-settings.json'])
def test_nested_private_configuration_blocks_source_archive(tmp_path, filename):
    root = fixture_source(tmp_path)
    (root / 'src' / filename).write_text('synthetic private setting', encoding='utf-8')
    target = root / 'dist/blocked.zip'
    with pytest.raises(subprocess.CalledProcessError):
        build(root, target)
    assert not target.exists()


def test_example_environment_can_be_delivered(tmp_path):
    root = fixture_source(tmp_path)
    (root / '.env.example').write_text('OPTIONAL_API_KEY=\n', encoding='utf-8')
    with zipfile.ZipFile(build(root)) as archive:
        assert '.env.example' in archive.namelist()
