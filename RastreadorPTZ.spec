# -*- mode: python ; coding: utf-8 -*-
# Receita do PyInstaller para gerar o RastreadorPTZ.exe (não editar à mão,
# a menos que saiba o que está fazendo). É usada pelo 1-COMPILAR.bat.
import os
from PyInstaller.utils.hooks import collect_all, collect_submodules, copy_metadata

bloco = None
raiz = os.path.abspath(os.getcwd())

# Bibliotecas pesadas que precisam levar TODOS os arquivos internos junto,
# senão o .exe abre e quebra por falta de dados/plugins.
datas, binaries, hiddenimports = [], [], []
for pacote in ("ultralytics", "torch", "torchvision", "cv2", "pygame",
               "matplotlib", "pandas", "scipy"):
    try:
        d, b, h = collect_all(pacote)
        datas += d; binaries += b; hiddenimports += h
    except Exception:
        pass
hiddenimports += collect_submodules("ultralytics")

# MUITO IMPORTANTE: o ultralytics (IA) lê em tempo de execução os METADADOS destes
# pacotes (versão instalada). O PyInstaller não inclui isso por padrão, e sem eles a
# IA quebra ("Falha ao carregar o modelo") mesmo com o resto do app funcionando.
for meta in ("ultralytics", "torch", "torchvision", "numpy", "opencv-python",
             "pyyaml", "tqdm", "psutil", "py-cpuinfo", "requests", "pillow",
             "matplotlib", "pandas", "scipy", "ultralytics-thop"):
    try:
        datas += copy_metadata(meta)
    except Exception:
        pass

# Submódulos extras que costumam faltar no empacotamento da IA.
for extra in ("torchvision", "PIL", "yaml", "tqdm", "psutil"):
    try:
        hiddenimports += collect_submodules(extra)
    except Exception:
        pass

# Arquivos que acompanham o programa (config padrão, modelo de IA, ícone).
for nome in ("config.ini", "yolov8n-pose.pt", "icone.ico", "LEIA-ME.md"):
    if os.path.exists(os.path.join(raiz, nome)):
        datas.append((nome, "."))

a = Analysis(
    ["ptz_app.py"],
    pathex=[raiz],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "PyQt5", "PyQt6"],   # matplotlib é NECESSÁRIO p/ a IA
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="RastreadorPTZ",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,                       # sem janela preta de terminal
    icon="icone.ico" if os.path.exists("icone.ico") else None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="RastreadorPTZ",                # gera dist\RastreadorPTZ\
)
