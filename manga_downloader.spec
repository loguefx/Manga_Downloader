# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for Manga Downloader
# Produces a single self-contained EXE: dist/MangaDownloader.exe
# Templates, static files and scrapers are embedded inside the EXE and
# extracted to sys._MEIPASS at runtime (handled by paths.py).

block_cipher = None

a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('templates',  'templates'),   # Flask HTML templates
        ('static',     'static'),      # CSS / JS assets
        ('scrapers',   'scrapers'),    # scraper package
        ('config.yaml', '.'),          # seed config bundled into EXE
    ],
    hiddenimports=[
        # Flask internals
        'flask',
        'flask.templating',
        'jinja2',
        'jinja2.ext',
        'jinja2.filters',
        'jinja2.utils',
        'jinja2.environment',
        'werkzeug',
        'werkzeug.serving',
        'werkzeug.routing',
        'werkzeug.exceptions',
        # Project modules
        'paths',
        'downloader',
        'scheduler',
        'mangadex_api',
        'scrapers',
        'scrapers.generic_site',
        'scrapers.onepiece',
        # Dependencies
        'yaml',
        'requests',
        'PIL',
        'PIL.Image',
        'PIL.JpegImagePlugin',
        'bs4',
        'schedule',
        'tqdm',
        # Windows Service — required
        'win32serviceutil',
        'win32service',
        'win32event',
        'win32api',
        'win32con',
        'pywintypes',
        'servicemanager',
        # subprocess (used by auto-service-start)
        'subprocess',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'tkinter',
        'matplotlib',
        'numpy',
        'scipy',
        'pandas',
        'IPython',
        'notebook',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# --onefile: embed everything into a single EXE
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='MangaDownloader',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,           # keep console so users can see log output
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)
