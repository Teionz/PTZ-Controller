# Rastreador PTZ — seguir uma pessoa automaticamente

App que segue uma pessoa escolhida com a câmera PTZ (PTZOptics / NeoiD),
usando IA de detecção de **corpo** (funciona de frente, de lado e de costas).
Você **clica na pessoa** e a câmera passa a segui-la sozinha.

O OBS continua recebendo o vídeo da câmera do mesmo jeito de hoje (NDI).
Este app conversa com a câmera **em paralelo**, pela rede.

---

## Passo a passo (no PC da igreja)

### 1. Ter o Python instalado
Se ainda não tiver: baixe o **Python 3.11** em https://www.python.org/downloads/
Na instalação, **marque a caixa "Add Python to PATH"**.

### 2. Instalar os componentes
Dê **duplo clique** em **`1-INSTALAR.bat`** e aguarde terminar.
(Só precisa fazer isso uma vez. Baixa a IA e o leitor de vídeo.)

### 3. Conferir o endereço da câmera
Abra o **`config.ini`** (com o Bloco de Notas) e confira:
- `ip` = o IP fixo da câmera (hoje é `10.0.0.250`)
- `rtsp_url` = endereço do vídeo. Começa como `.../554/2`.
  Se não aparecer vídeo, troque o **2** por **1** e teste de novo.

### 4. Usar
Dê **duplo clique** em **`2-INICIAR.bat`** — abre o app profissional, com o
vídeo à esquerda e os painéis à direita (rastreamento, ponto central,
velocidade, presets, cores e exposição).

> Se por algum motivo a versão profissional der problema, use a reserva
> **`2b-INICIAR-versao-simples.bat`** (só a janela de vídeo).

**Controle de PS4** (conecte o controle antes de abrir):
| Comando | O que faz |
|---|---|
| Analógico esquerdo | giro / inclinação (pan/tilt) |
| Analógico direito | cima/baixo = zoom · esquerda/direita = foco |
| X | preset 1 · ○ preset 2 · □ preset 3 · △ preset 4 |
| Seta ↓ | salva preset 1 · → salva 2 · ← salva 3 · ↑ salva 4 |
| R1 | para de seguir · R2 volta a seguir |
| L1 | cancela seguir · L2 segue a pessoa central |

> Se algum botão fizer a coisa errada, dá pra ajustar os números no `config.ini`
> (seção `[gamepad]`) — me diga qual botão e eu acerto.



**Controles — rastreamento automático:**
| Tecla / ação | O que faz |
|---|---|
| **Clique** na pessoa | escolhe quem seguir |
| **N** | troca para a próxima pessoa |
| **C** | cancela (câmera para) |
| **Espaço** | pausa / retoma |
| **+ / −** | mais / menos sensível (rápido) |
| **ESC** | sai (para a câmera com segurança) |

**Controles — manual (mover a câmera na mão):**
| Tecla | O que faz |
|---|---|
| **W / S** | inclina para cima / baixo |
| **A / D** | gira para esquerda / direita |
| **Q / E** | zoom aproxima / afasta |

> Ao usar W/A/S/D/Q/E o app entra em **MANUAL** e para de seguir enquanto a
> tecla estiver pressionada (dá pra ir na diagonal, ex: W+D). Soltou, parou.
> Para assumir o controle de vez, aperte **C** antes (cancela o alvo).
> **A janela do app precisa estar na frente (em foco)** para o teclado mover a
> câmera — isso evita mexer na câmera sem querer enquanto você usa outro programa.

**Presets (posições salvas na câmera):**
| Ação | O que faz |
|---|---|
| **0 a 9** | move a câmera para o preset salvo |
| **P** e depois um número | salva a posição atual naquele número |

---

## PRIMEIRO TESTE (faça nesta ordem e anote o resultado)

1. **Apareceu vídeo?**
   - Sim → ótimo.
   - Não → no `config.ini`, troque `/1` por `/2` no `rtsp_url` e tente de novo.

2. **Clicou numa pessoa e a câmera se moveu para segui-la?**
   - Sim → ótimo, o controle funciona.
   - Não mexeu nada → no `config.ini` troque `protocolo = visca` por `protocolo = http`.
   - Mexeu para o lado ERRADO → veja "Se ela mexer para o lado errado" abaixo.

3. **Teste o manual:** segure **W A S D** (move) e **Q E** (zoom).
4. **Teste preset:** aperte **P** e depois **1** (salva). Mova a câmera. Aperte **1** (deve voltar).

Anote: (a) apareceu vídeo? (b) a câmera mexeu? (c) usou `visca` ou `http`?
(d) a direção estava certa? — e me mande esses 4 pontos.

---

## Se a câmera NÃO mexer

1. Abra o `config.ini`.
2. Na seção `[controle]`, troque a linha:
   `protocolo = visca`  →  `protocolo = http`
3. Salve e abra o `2-INICIAR.bat` de novo.

## Se ela mexer para o lado ERRADO
No `config.ini`, seção `[controle]`, troque `false` por `true` em
`inverter_pan` (esquerda/direita) ou `inverter_tilt` (cima/baixo).

## Se ficar tremendo ou nervoso
No `config.ini`, seção `[rastreamento]`:
- aumente `zona_morta_x` e `zona_morta_y` (ex: de 0.06 para 0.10)
- ou diminua `ganho_pan` e `ganho_tilt`

## Se travar / vídeo picotado
No `config.ini`, diminua `largura_processamento` (ex: 960 → 640).

---

## Observações
- O app **não** grava nem transmite nada. Só lê o vídeo e move a câmera.
- Ao fechar, ele sempre envia o comando de **parar** para a câmera.
- Da primeira vez que rodar, ele baixa o modelo de IA (~6 MB) automaticamente.
