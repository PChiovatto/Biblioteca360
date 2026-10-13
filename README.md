# Biblioteca 360

Biblioteca de jogos de Xbox para o PC, no estilo do app Xbox.

- **Xbox 360:** jogos Games on Demand, Arcade, pastas com `default.xex` e imagens `.iso`, abertos no
  Xenia Canary dentro da própria janela. Lê o HD ou pendrive do Xbox 360 (FATX, só leitura) e instala os jogos no PC.
- **Xbox original:** imagens `.iso`/`.xiso` abertas no xemu, dentro da janela. O app baixa o xemu e um HD
  vazio; a BIOS (flash) e a MCPX (boot ROM) precisam vir do seu próprio console.
- **Xbox One / Series:** não há emulador. O app lista os jogos para PC instalados pelo app Xbox
  (pastas `X:\XboxGames`) e abre pelo Windows, e tem atalhos para o app Xbox e o Xbox Cloud Gaming.
- Nome do perfil do Xenia: escrito com o controle em Configurações e digitado no jogo com Back + Y.
- Página "Este PC" avalia se o computador aguenta o emulador.
- Funciona com controle Xbox (XInput).

## Arquivos

- `xbox360_launcher.py`: o app (interface Tkinter).
- `fatx.py`: leitor do HD do Xbox, executado como administrador só para leitura.
- `LEIA-ME.txt`: instruções para o usuário final.

O código-fonte da versão 1 foi reconstruído a partir do `Biblioteca360.exe`
(cada função conferida contra o bytecode original, Python 3.12). As funções de
Xbox original, Xbox One/Series e nome do perfil foram acrescentadas depois.

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
