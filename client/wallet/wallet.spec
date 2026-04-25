# -*- mode: python ; coding: utf-8 -*-

a = Analysis(
    ['wallet.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[
        # ── Project modules (not auto-detected because they are imported
        #    dynamically or via plain `import wire` style) ──────────────
        'wire',
        'tx_codec',
        'block_header',
        'merkle',
        'gpu_miner',

        # ── Cryptography ──────────────────────────────────────────────
        'ecdsa',
        'ecdsa.keys',
        'ecdsa.curves',
        'ecdsa.ellipticcurve',
        'ecdsa.numbertheory',
        'ecdsa.rfc6979',
        'ecdsa.ecdh',

        # ── Numeric / GPU ─────────────────────────────────────────────
        'numpy',
        'numpy.core',
        'numpy.core._multiarray_umath',

        # ── pycuda (optional — NVIDIA only) ───────────────────────────
        # PyInstaller cannot bundle the native CUDA DLLs, but we need
        # the Python wrapper modules in the exe so the import succeeds
        # and the graceful-fallback path runs instead of crashing.
        'pycuda',
        'pycuda.driver',
        'pycuda.compiler',
        'pycuda.autoinit',

        # ── pyopencl (optional — AMD / Intel / NVIDIA) ────────────────
        'pyopencl',

        # ── HTTP / stdlib extras ──────────────────────────────────────
        'requests',
        'requests.adapters',
        'requests.packages',
        'urllib3',
        'certifi',
        'charset_normalizer',
        'idna',

        # ── PySide6 sub-modules that are sometimes missed ─────────────
        'PySide6.QtCore',
        'PySide6.QtGui',
        'PySide6.QtWidgets',

        # ── stdlib modules that faulthandler / ctypes need ────────────
        'faulthandler',
        'ctypes',
        'ctypes.util',
        'glob',
        'struct',
        'hashlib',
        'threading',
        'json',
        'io',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='wallet',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    # Exclude pycuda / pyopencl native binaries from UPX compression —
    # UPX can corrupt CUDA driver DLLs on some Windows builds.
    upx_exclude=[
        'vcruntime140.dll',
        'python3*.dll',
        'nvrtc*.dll',
        'cuda*.dll',
        '_cl.*.pyd',
        '_pycuda*.pyd',
    ],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
