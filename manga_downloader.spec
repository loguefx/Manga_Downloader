# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for Manga Downloader
# Produces a one-directory bundle: dist/MangaDownloader/MangaDownloader.exe

block_cipher = None

a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('templates',  'templates'),   # Flask HTML templates
        ('static',     'static'),      # CSS / JS assets
        ('scrapers',   'scrapers'),    # scraper package
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
        # Windows Service (optional — only needed if running as a service)
        'win32serviceutil',
        'win32service',
        'win32event',
        'servicemanager',
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

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='MangaDownloader',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,           # keep console so users can see log output
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='MangaDownloader',
)
