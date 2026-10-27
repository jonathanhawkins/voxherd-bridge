# -*- mode: python ; coding: utf-8 -*-
import os

block_cipher = None

bridge_dir = os.path.join(os.path.dirname(SPEC), '..', 'bridge')

a = Analysis(
    [os.path.join(bridge_dir, '__main__.py')],
    pathex=[os.path.join(bridge_dir, '..')],
    binaries=[],
    datas=[
        (os.path.join(bridge_dir, 'stt', 'voxherd-listen'), 'bridge/stt'),
    ],
    hiddenimports=[
        'bridge',
        'bridge.cli',
        'bridge.bridge_server',
        'bridge.session_manager',
        'bridge.mac_tts',
        'bridge.mac_stt',
        'bridge.mac_voice_loop',
        'bridge.tmux_manager',
        'bridge.task_store',
        'bridge.sub_agent_monitor',
        'bridge.session_status',
        'bridge.choice_detector',
        'bridge.bonjour',
        'bridge.tailscale',
        'bridge.env_utils',
        'bridge.reader',
        'bridge.focus',
        'bridge.pairing',
        'bridge.pane_geometry',
        # The Reader's grammars are imported by name, so nothing finds them statically.
        'tree_sitter',
        'tree_sitter_python',
        'tree_sitter_javascript',
        'tree_sitter_typescript',
        'tree_sitter_kotlin',
        'tree_sitter_swift',
        'tree_sitter_go',
        'tree_sitter_rust',
        'tree_sitter_java',
        'uvicorn',
        'uvicorn.logging',
        'uvicorn.loops',
        'uvicorn.loops.auto',
        'uvicorn.loops.asyncio',
        'uvicorn.protocols',
        'uvicorn.protocols.http',
        'uvicorn.protocols.http.auto',
        'uvicorn.protocols.http.h11_impl',
        'uvicorn.protocols.http.httptools_impl',
        'uvicorn.protocols.websockets',
        'uvicorn.protocols.websockets.auto',
        'uvicorn.protocols.websockets.wsproto_impl',
        'uvicorn.protocols.websockets.websockets_impl',
        'uvicorn.lifespan',
        'uvicorn.lifespan.on',
        'uvicorn.lifespan.off',
        'uvicorn.main',
        'uvicorn.config',
        'uvicorn.server',
        'fastapi',
        'starlette',
        'starlette.routing',
        'starlette.responses',
        'starlette.websockets',
        'pydantic',
        'anyio',
        'anyio._backends',
        'anyio._backends._asyncio',
        'httptools',
        'uvloop',
        'watchfiles',
        'websockets',
        'wsproto',
        'h11',
        'httpx',
        'httpcore',
        'rich',
        'zeroconf',
        # Named rather than left to the analysis: `bridge/bonjour.py` imports
        # it inside a function, behind a try/except that degrades to one
        # address. A bundle that missed it would advertise a single interface
        # and say nothing about why -- which is the bug that import exists to
        # fix.
        'ifaddr',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
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
    name='voxherd-bridge',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    target_arch='arm64',
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name='voxherd-bridge',
)
