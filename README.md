# PTZ Controller

Rastreador automático de pessoa para câmeras PTZ (linha PTZOptics / NeoiD), feito
para seguir o pregador durante o culto sem operador. O operador **clica na pessoa**
e a câmera passa a segui-la sozinha — de frente, de lado ou de costas.

Feito em Python + PySide6 (Qt), com IA de detecção de pose (YOLO / Ultralytics) e
controle da câmera por **VISCA sobre IP** (UDP 1259) ou HTTP CGI. O OBS continua
recebendo o NDI da câmera normalmente; este app roda em paralelo, lendo o RTSP.

## Recursos

- **Seguir por clique** com rastreamento estável (mira nos ombros/cabeça — braço
  aberto não desloca o enquadramento) e "velocidade de seguir" que acelera quando
  a pessoa se aproxima da borda, para não perder quem anda rápido.
- **Presets nomeados** (posição + zoom + foco + cores/exposição), com atalhos
  `Ctrl+1..9` em qualquer tela.
- **Controle manual** por teclado (W/A/S/D + Q/E) e **gamepad PS4/Xbox**.
- **Painéis encaixáveis** estilo OBS, com segunda janela para outro monitor.
- **Cores / exposição / white balance / foco** ajustáveis (VISCA + HTTP), com
  ajuste **fino de foco** por toques (para tirar o foco do telão de LED).
- **Menu OSD** da câmera acessível pelo app.

## Requisitos

- Windows 10/11
- Python 3.11 (para rodar pelo código) — marque *Add Python to PATH* na instalação
- A câmera PTZ na mesma rede, com stream RTSP e VISCA habilitados

## Como usar

### A) Rodar direto pelo Python (desenvolvimento / teste rápido)

1. `1-INSTALAR.bat` — instala as dependências (uma vez).
2. `2-INICIAR.bat` — abre o programa.

### B) Gerar o instalador profissional (.exe)

1. `1-COMPILAR.bat` — empacota o programa em `dist\RastreadorPTZ\` (PyInstaller).
2. `2-CRIAR-INSTALADOR.bat` — gera `instalador_saida\RastreadorPTZ_Setup.exe`
   (precisa do [Inno Setup](https://jrsoftware.org/isdl.php) instalado — gratuito).

O `Setup.exe` instala em *Arquivos de Programas*, cria atalhos no Menu Iniciar e
na Área de Trabalho e registra o desinstalador em *Adicionar ou remover programas*.

## Configuração

- `config.ini` — IP da câmera, RTSP, protocolo, sensibilidade do rastreio, presets,
  gamepad, etc. (comentado em português). Quando instalado, uma cópia editável fica
  em `%APPDATA%\RastreadorPTZ\`.
- Dados de tempo de execução (presets, cores salvas, layout) ficam em
  `%APPDATA%\RastreadorPTZ\estado.json`.

## Diagnóstico

- `DIAGNOSTICO.bat` — testa qual porta/protocolo a câmera responde (VISCA/HTTP) e
  imprime as posições de íris/shutter. Útil quando a câmera não responde a algum
  comando.

## Estrutura

| Arquivo | O que é |
|---|---|
| `ptz_app.py` | Aplicativo principal (interface Qt, rastreamento, painéis). |
| `ptz_tracker.py` | Controle da câmera (VISCA/HTTP), leitura de vídeo, caminhos. |
| `config.ini` | Configuração padrão (comentada). |
| `RastreadorPTZ.spec` | Receita do PyInstaller. |
| `instalador.iss` | Script do instalador (Inno Setup). |
| `1-*.bat` / `2-*.bat` | Passos de instalar / rodar / compilar / empacotar. |

## Licença

Uso interno. Todos os direitos reservados.
