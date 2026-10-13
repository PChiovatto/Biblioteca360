# Biblioteca 360

Biblioteca de jogos de Xbox 360 para o PC, no estilo do app Xbox.

- Lista os jogos instalados no PC (Games on Demand, Arcade e pastas com `default.xex`).
- Lê o HD ou pendrive do Xbox 360 (FATX, somente leitura) e instala os jogos no PC.
- Abre os jogos no Xenia Canary dentro da própria janela.
- Página "Este PC" avalia se o computador aguenta o emulador.
- Funciona com controle Xbox (XInput).

## Arquivos

- `xbox360_launcher.py`: o app (interface Tkinter).
- `fatx.py`: leitor do HD do Xbox, executado como administrador só para leitura.
- `LEIA-ME.txt`: instruções para o usuário final.

O código-fonte foi reconstruído a partir do `Biblioteca360.exe`. Cada função foi
conferida contra o bytecode original (Python 3.12) e é idêntica; só comentários e
numeração de linhas não puderam ser recuperados.

## Rodar a partir do código (Windows 10/11, 64 bits)

```powershell
py -3.12 -m pip install -r requirements.txt
py -3.12 xbox360_launcher.py
```

## Gerar o Biblioteca360.exe

```powershell
py -3.12 -m pip install pyinstaller
py -3.12 -m PyInstaller --noconfirm --windowed --name Biblioteca360 --hidden-import fatx xbox360_launcher.py
```

O programa fica em `dist\Biblioteca360\`. Copie a pasta inteira para `C:\Biblioteca360`.
