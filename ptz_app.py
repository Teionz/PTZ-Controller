# -*- coding: utf-8 -*-
"""
Rastreador PTZ - App profissional (janela com painéis).

- Vídeo à esquerda (clique numa pessoa para seguir).
- Painéis à direita: Rastreamento, Ponto central, Velocidade, Presets,
  Cores e Exposição/White Balance.
- Controle manual pelo teclado (W/A/S/D + Q/E) e por controle de PS4 / Xbox.

Rode por: 2-INICIAR.bat
"""

import base64
import json
import os
import sys
import threading
import time

import cv2
import numpy as np

from PySide6 import QtCore, QtGui, QtWidgets

from ptz_tracker import (ControladorCamera, LeitorVideo, carregar_config,
                         caminho_modelo, pasta_dados)

try:
    import pygame
    _HAS_PYGAME = True
except Exception:
    _HAS_PYGAME = False

PASTA = os.path.dirname(os.path.abspath(__file__))
# estado.json vai para a pasta GRAVÁVEL (em %APPDATA% quando instalado como .exe;
# a própria pasta quando rodando pelo Python) — Arquivos de Programas é read-only.
ESTADO_JSON = os.path.join(pasta_dados(), "estado.json")

# Tabelas (aproximadas, padrão Sony/PTZOptics 60Hz) para mostrar shutter/íris
# como no app da PTZOptics. Se algum valor não bater com o que a câmera mostra,
# é só ajustar aqui — o número da posição (pos N) sempre aparece do lado.
# Tabela oficial Sony/PTZOptics (60Hz). Íris no painel do usuário: F5.0/F2.2.
# CONFERIDO contra o painel real da câmera (2026-08-23): a posição 1 é o 1º valor
# da lista e sobe dali — NÃO existe posição 0.
SHUTTER_STR = {
    0x01: "1/30", 0x02: "1/60", 0x03: "1/90", 0x04: "1/100", 0x05: "1/125",
    0x06: "1/180", 0x07: "1/250", 0x08: "1/350", 0x09: "1/500", 0x0A: "1/725",
    0x0B: "1/1000", 0x0C: "1/1500", 0x0D: "1/2000", 0x0E: "1/3000",
    0x0F: "1/4000", 0x10: "1/6000", 0x11: "1/10000",
}
SHUTTER_MIN, SHUTTER_MAX = min(SHUTTER_STR), max(SHUTTER_STR)
# Íris: posição 1 = FECHADA (escuro), sobe conforme vai ABRINDO, até posição 13
# = F1.8 (mais aberta/clara). Bate com iris_mais() somando à posição (abre).
# Corrigido 2026-08-23: estava invertido (usuário confirmou testando na câmera).
IRIS_STR = {
    0x01: "Fechada", 0x02: "F11", 0x03: "F9.6", 0x04: "F8.0", 0x05: "F6.8",
    0x06: "F5.6", 0x07: "F4.8", 0x08: "F4.0", 0x09: "F3.4", 0x0A: "F2.8",
    0x0B: "F2.4", 0x0C: "F2.0", 0x0D: "F1.8",
}
IRIS_MIN, IRIS_MAX = min(IRIS_STR), max(IRIS_STR)


def _rotulo_valor(tabela, pos):
    if pos is None:
        return "—"
    s = tabela.get(pos)
    return ("%s  (pos %d)" % (s, pos)) if s else ("pos %d" % pos)


# ============================================================================
# Estado do app (nomes de preset e cores) - guardado à parte do config.ini
# ============================================================================
def carregar_estado(cfg):
    estado = {
        "conexao": {},       # ip/rtsp/protocolo/portas configurados pelo menu do app
        "layout": {},        # disposição dos painéis/janela (posição, tamanho, abertos)
        "rastreamento": {},  # ajustes de rastreamento feitos pelos sliders (salvos)
        "presets": {str(i): cfg.get("presets", "nome_%d" % i, fallback="Preset %d" % i)
                    for i in range(1, 10)},
        "presets_cfg": {},   # por preset: {"cores": {...}, "foco_auto": bool}
        "cores": {
            "saturacao": cfg.getint("cores", "saturacao", fallback=50),
            "contraste": cfg.getint("cores", "contraste", fallback=50),
            "brilho": cfg.getint("cores", "brilho", fallback=50),
            "matiz": cfg.getint("cores", "matiz", fallback=50),
            "nitidez": cfg.getint("cores", "nitidez", fallback=50),
            "white_balance": cfg.get("cores", "white_balance", fallback="none"),
        },
    }
    if os.path.exists(ESTADO_JSON):
        try:
            with open(ESTADO_JSON, "r", encoding="utf-8") as f:
                d = json.load(f)
            estado["conexao"].update(d.get("conexao", {}))
            estado["layout"].update(d.get("layout", {}))
            estado["rastreamento"].update(d.get("rastreamento", {}))
            estado["presets"].update(d.get("presets", {}))
            estado["presets_cfg"].update(d.get("presets_cfg", {}))
            estado["cores"].update(d.get("cores", {}))
        except Exception as e:
            print("Aviso: não consegui ler estado.json:", e)
    return estado


def salvar_estado(estado):
    try:
        with open(ESTADO_JSON, "w", encoding="utf-8") as f:
            json.dump(estado, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("Aviso: não consegui salvar estado.json:", e)


# ============================================================================
# Thread de detecção (IA) - roda separada para a interface não travar
# ============================================================================
class Detector(QtCore.QThread):
    resultado = QtCore.Signal(object)   # lista de (id, x1, y1, x2, y2)
    aviso = QtCore.Signal(str)

    def __init__(self, get_frame, proc_w, det_w, modelo_nome, conf=0.35):
        super().__init__()
        self.get_frame = get_frame
        self.proc_w = proc_w        # tamanho de EXIBIÇÃO (caixas saem neste espaço)
        self.det_w = det_w          # tamanho de DETECÇÃO (menor = mais rápido)
        self.modelo_nome = modelo_nome
        self.conf = conf
        self.rodando = True
        self.ativo = True
        self._erro_frame_logado = False

    def _log_erro(self, titulo, e):
        """Grava o erro completo da IA num arquivo, pra dar pra diagnosticar quando
        roda como .exe instalado (onde não há tela preta de terminal pra ver)."""
        import traceback
        msg = "%s: %s" % (titulo, e)
        try:
            caminho = os.path.join(pasta_dados(), "erro_ia.txt")
            with open(caminho, "w", encoding="utf-8") as f:
                f.write(msg + "\n\n")
                f.write(traceback.format_exc())
            msg += "  (detalhes em %s)" % caminho
        except Exception:
            pass
        return msg

    def run(self):
        try:
            from ultralytics import YOLO
        except Exception as e:
            self.aviso.emit(self._log_erro("IA indisponível (dependências)", e))
            return
        try:
            modelo = YOLO(caminho_modelo(self.modelo_nome))
        except Exception as e:
            self.aviso.emit(self._log_erro("Falha ao carregar o modelo", e))
            return
        usa_pose = "pose" in self.modelo_nome.lower()
        self.aviso.emit("IA pronta." + (" (pose)" if usa_pose else ""))
        while self.rodando:
            if not self.ativo:
                self.msleep(30)
                continue
            frame = self.get_frame()
            if frame is None:
                self.msleep(10)
                continue
            h0, w0 = frame.shape[:2]
            # detecta numa resolução menor (rápido); vídeo fica em Full HD à parte
            escd = self.det_w / float(w0)
            fr = cv2.resize(frame, (self.det_w, max(1, int(h0 * escd))))
            try:
                res = modelo.track(fr, persist=True, classes=[0],
                                   tracker="bytetrack.yaml", verbose=False,
                                   imgsz=self.det_w, conf=self.conf)
            except Exception as e:
                # loga só a 1a vez (senão escreveria no disco a cada quadro, ~30x/s)
                if not self._erro_frame_logado:
                    self._erro_frame_logado = True
                    self.aviso.emit(self._log_erro("Erro na IA durante a detecção", e))
                else:
                    self.aviso.emit("Erro na IA: %s" % e)
                self.msleep(100)
                continue

            fator = self.proc_w / float(self.det_w)
            dados = []   # (id, x1, y1, x2, y2, aim_x, aim_y) já no espaço de exibição
            r0 = res[0] if res else None
            if r0 is not None and r0.boxes is not None and r0.boxes.id is not None:
                ids = r0.boxes.id.cpu().numpy().astype(int)
                xy = r0.boxes.xyxy.cpu().numpy()
                kxy = kconf = None
                if usa_pose and r0.keypoints is not None and r0.keypoints.xy is not None:
                    kxy = r0.keypoints.xy.cpu().numpy()        # (N,17,2)
                    if r0.keypoints.conf is not None:
                        kconf = r0.keypoints.conf.cpu().numpy()  # (N,17)
                for i, b in enumerate(xy):
                    x1, y1, x2, y2 = b
                    alt = max(1.0, y2 - y1)
                    # mira padrão (sem pose confiável): centro X, cabeça perto do topo.
                    ax = (x1 + x2) / 2.0
                    ay = y1 + alt * 0.12
                    aim_ok = False   # a mira só é confiável se vier da POSE (rosto/ombros)
                    if kxy is not None and i < len(kxy):
                        k = kxy[i]
                        cf = kconf[i] if kconf is not None else [1.0] * len(k)

                        def val(j):
                            return (j < len(k) and cf[j] > 0.30 and
                                    (k[j][0] > 0 or k[j][1] > 0))
                        ombros = val(5) and val(6)
                        # X = meio dos OMBROS (braço não desloca a mira); senão nariz
                        if ombros:
                            ax = (k[5][0] + k[6][0]) / 2.0
                            aim_ok = True
                        elif val(0):
                            ax = k[0][0]
                            aim_ok = True
                        # Y = CABEÇA (nariz); senão estima acima dos ombros
                        if val(0):
                            ay = k[0][1]
                        elif ombros:
                            ay = (k[5][1] + k[6][1]) / 2.0 - 0.30 * alt
                    dados.append((int(ids[i]), int(x1 * fator), int(y1 * fator),
                                  int(x2 * fator), int(y2 * fator),
                                  int(ax * fator), int(ay * fator), aim_ok))
            self.resultado.emit(dados)

    def parar(self):
        self.rodando = False
        self.wait(1500)


# ============================================================================
# Thread que lê as configurações atuais da câmera (não trava a interface)
# ============================================================================
class LeitorConfig(QtCore.QThread):
    pronto = QtCore.Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.camera = None

    def run(self):
        try:
            d = self.camera.ler_configuracoes(timeout=0.4) if self.camera else {}
        except Exception as e:
            print("Erro ao ler config da câmera:", e)
            d = {}
        self.pronto.emit(d)


class EsperaChegada(QtCore.QThread):
    """Espera a câmera CHEGAR no preset lendo a posição do zoom até ela parar de
    mudar. Se a câmera não informar a posição, usa o atraso fixo (fallback)."""
    chegou = QtCore.Signal()

    def __init__(self, camera, delay_fallback, parent=None):
        super().__init__(parent)
        self.camera = camera
        self.delay = delay_fallback

    def run(self):
        primeira = self.camera.ler_pos_zoom()
        if primeira is None:                     # câmera não informa posição -> atraso fixo
            time.sleep(max(0.2, self.delay))
            self.chegou.emit()
            return
        t0 = time.time()
        ultimo = primeira
        estavel = 0
        while time.time() - t0 < 6.0:            # segurança
            time.sleep(0.15)
            pos = self.camera.ler_pos_zoom()
            if pos is None:
                continue
            if abs(pos - ultimo) <= 3:           # praticamente parado
                estavel += 1
                if estavel >= 2:                 # estável por ~0.3s = chegou
                    break
            else:
                estavel = 0
            ultimo = pos
        time.sleep(0.2)                          # respiro pra assentar
        self.chegou.emit()


# ============================================================================
# Widget de vídeo (mostra o frame e avisa onde o usuário clicou)
# ============================================================================
class VideoLabel(QtWidgets.QLabel):
    clicado = QtCore.Signal(int, int)      # clique esquerdo: seleciona pessoa
    foco_aqui = QtCore.Signal(int, int)    # clique direito: focar ali e travar

    def __init__(self):
        super().__init__()
        self.setMinimumSize(640, 360)
        self.setAlignment(QtCore.Qt.AlignCenter)
        self.setStyleSheet("background:#0d0d0d;")
        self._iw = self._ih = 0
        self._dw = self._dh = 0
        self._ox = self._oy = 0

    def mostrar(self, img_bgr):
        h, w = img_bgr.shape[:2]
        self._iw, self._ih = w, h
        rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
        rgb = np.ascontiguousarray(rgb)
        qimg = QtGui.QImage(rgb.data, w, h, 3 * w, QtGui.QImage.Format_RGB888)
        pm = QtGui.QPixmap.fromImage(qimg)
        lw, lh = max(1, self.width()), max(1, self.height())
        scale = min(lw / w, lh / h)
        self._dw, self._dh = int(w * scale), int(h * scale)
        self._ox = (lw - self._dw) // 2
        self._oy = (lh - self._dh) // 2
        self.setPixmap(pm.scaled(self._dw, self._dh, QtCore.Qt.KeepAspectRatio,
                                 QtCore.Qt.SmoothTransformation))

    def mousePressEvent(self, ev):
        if self._dw <= 0:
            return
        x = ev.position().x() - self._ox
        y = ev.position().y() - self._oy
        if not (0 <= x < self._dw and 0 <= y < self._dh):
            return
        ix = int(x / self._dw * self._iw)
        iy = int(y / self._dh * self._ih)
        if ev.button() == QtCore.Qt.RightButton:
            self.foco_aqui.emit(ix, iy)     # botão direito = focar ali
        else:
            self.clicado.emit(ix, iy)       # botão esquerdo = seleciona pessoa


# ============================================================================
# Leitor de controle PS4 / Xbox
# ============================================================================
class Gamepad:
    def __init__(self, cfg=None, ativar=True):
        self.ok = False
        self.js = None
        self._botoes_ant = {}
        self._hat_ant = (0, 0)
        # mapeamento (índices) — ajustáveis no config.ini [gamepad] se o modelo diferir
        g = (lambda k, d: cfg.getint("gamepad", k, fallback=d)) if cfg else (lambda k, d: d)
        self.zona = (cfg.getfloat("gamepad", "zona_morta", fallback=0.15) if cfg else 0.15)
        self.eixo_pan = g("eixo_pan", 0)     # analógico esquerdo X
        self.eixo_tilt = g("eixo_tilt", 1)   # analógico esquerdo Y
        self.eixo_foco = g("eixo_foco", 2)   # analógico direito X
        self.eixo_zoom = g("eixo_zoom", 3)   # analógico direito Y
        self.bt_preset1 = g("btn_preset1", 0)   # X (cross)
        self.bt_preset2 = g("btn_preset2", 1)   # O (circle)
        self.bt_preset3 = g("btn_preset3", 2)   # quadrado
        self.bt_preset4 = g("btn_preset4", 3)   # triângulo
        self.bt_cancela = g("btn_l1", 4)        # L1 = cancela seguir
        self.bt_pausa = g("btn_r1", 5)          # R1 = para de seguir
        self.bt_central = g("btn_l2", 6)        # L2 = segue central
        self.bt_resume = g("btn_r2", 7)         # R2 = volta a seguir
        if not ativar or not _HAS_PYGAME:
            return
        try:
            pygame.init()
            pygame.joystick.init()
            if pygame.joystick.get_count() > 0:
                self.js = pygame.joystick.Joystick(0)
                self.js.init()
                self.ok = True
        except Exception as e:
            print("Gamepad indisponível:", e)

    def nome(self):
        return self.js.get_name() if self.ok else "nenhum"

    def _eixo(self, i):
        try:
            v = self.js.get_axis(i)
        except Exception:
            return 0.0
        return 0.0 if abs(v) < self.zona else v

    def _botao(self, i):
        try:
            return self.js.get_button(i)
        except Exception:
            return 0

    def ler(self):
        """Movimento contínuo + eventos de borda (só no instante em que aperta)."""
        r = {"pan": 0.0, "tilt": 0.0, "zoom": 0.0, "foco": 0.0, "eventos": []}
        if not self.ok:
            return r
        try:
            pygame.event.pump()
        except Exception:
            return r

        # analógico esquerdo = pan/tilt ; direito = zoom (Y) e foco (X)
        r["pan"] = self._eixo(self.eixo_pan)
        r["tilt"] = -self._eixo(self.eixo_tilt)
        r["zoom"] = -self._eixo(self.eixo_zoom)   # cima = aproxima
        r["foco"] = self._eixo(self.eixo_foco)    # direita/esquerda

        def borda(idx, acao):
            atual = self._botao(idx)
            if atual and not self._botoes_ant.get(idx, 0):
                r["eventos"].append(acao)
            self._botoes_ant[idx] = atual

        borda(self.bt_preset1, "preset1")
        borda(self.bt_preset2, "preset2")
        borda(self.bt_preset3, "preset3")
        borda(self.bt_preset4, "preset4")
        borda(self.bt_cancela, "cancela")
        borda(self.bt_pausa, "pausa")
        borda(self.bt_central, "central")
        borda(self.bt_resume, "resume")

        # d-pad SALVA presets (baixo=1, direita=2, esquerda=3, cima=4)
        try:
            if self.js.get_numhats() > 0:
                hx, hy = self.js.get_hat(0)
                if (hx, hy) != self._hat_ant:
                    if hy == -1:
                        r["eventos"].append("salva1")
                    elif hx == 1:
                        r["eventos"].append("salva2")
                    elif hx == -1:
                        r["eventos"].append("salva3")
                    elif hy == 1:
                        r["eventos"].append("salva4")
                    self._hat_ant = (hx, hy)
        except Exception:
            pass
        return r


# ============================================================================
# Controles que IGNORAM a roda do mouse (só mudam quando clicados/focados).
# Evita alterar valores sem querer ao rolar a página com a rodinha.
# ============================================================================
class SliderSemRoda(QtWidgets.QSlider):
    def wheelEvent(self, e):
        if self.hasFocus():
            super().wheelEvent(e)
        else:
            e.ignore()   # deixa a página rolar em vez de mexer no valor


class ComboSemRoda(QtWidgets.QComboBox):
    def wheelEvent(self, e):
        if self.hasFocus():
            super().wheelEvent(e)
        else:
            e.ignore()


# ============================================================================
# Tela de Configurações de conexão (IP, protocolo, portas, etc.)
# ============================================================================
class DialogoConexao(QtWidgets.QDialog):
    def __init__(self, parent, cfg):
        super().__init__(parent)
        self.setWindowTitle("Configurações da câmera / conexão")
        self.setMinimumWidth(420)
        form = QtWidgets.QFormLayout(self)

        self.ed_ip = QtWidgets.QLineEdit(cfg.get("camera", "ip", fallback="10.0.0.250"))
        form.addRow("IP da câmera:", self.ed_ip)

        self.ed_rtsp = QtWidgets.QLineEdit(cfg.get("camera", "rtsp_url", fallback=""))
        form.addRow("Endereço do vídeo (RTSP):", self.ed_rtsp)

        self.cb_proto = ComboSemRoda()
        self.cb_proto.addItems(["visca", "http"])
        self.cb_proto.setCurrentText(cfg.get("controle", "protocolo", fallback="visca"))
        form.addRow("Protocolo de controle:", self.cb_proto)

        self.sp_visca = QtWidgets.QSpinBox()
        self.sp_visca.setRange(1, 65535)
        self.sp_visca.setValue(cfg.getint("controle", "visca_porta", fallback=1259))
        form.addRow("Porta VISCA:", self.sp_visca)

        self.sp_http = QtWidgets.QSpinBox()
        self.sp_http.setRange(1, 65535)
        self.sp_http.setValue(cfg.getint("controle", "http_porta", fallback=80))
        form.addRow("Porta HTTP:", self.sp_http)

        self.ck_inv_pan = QtWidgets.QCheckBox("Inverter giro (esquerda/direita)")
        self.ck_inv_pan.setChecked(cfg.getboolean("controle", "inverter_pan", fallback=False))
        form.addRow(self.ck_inv_pan)
        self.ck_inv_tilt = QtWidgets.QCheckBox("Inverter inclinação (cima/baixo)")
        self.ck_inv_tilt.setChecked(cfg.getboolean("controle", "inverter_tilt", fallback=False))
        form.addRow(self.ck_inv_tilt)

        self.sp_wbvar = QtWidgets.QSpinBox()
        self.sp_wbvar.setRange(0, 255)
        self.sp_wbvar.setValue(cfg.getint("cores", "wb_var_codigo", fallback=0x20))
        form.addRow("Código VISCA do modo VAR:", self.sp_wbvar)

        self.cb_res = ComboSemRoda()
        for r in ["1920", "1280", "960", "720"]:
            self.cb_res.addItem(r + " (largura)")
        atual = str(cfg.getint("rastreamento", "largura_processamento", fallback=1920))
        idx = ["1920", "1280", "960", "720"].index(atual) if atual in ["1920", "1280", "960", "720"] else 0
        self.cb_res.setCurrentIndex(idx)
        form.addRow("Resolução de processamento:", self.cb_res)

        self.sp_delay = QtWidgets.QDoubleSpinBox()
        self.sp_delay.setRange(0.0, 8.0)
        self.sp_delay.setSingleStep(0.5)
        self.sp_delay.setSuffix(" s")
        self.sp_delay.setValue(cfg.getfloat("presets", "delay_cores", fallback=2.0))
        self.sp_delay.setToolTip("O app troca as cores quando detecta que a câmera CHEGOU no "
                                 "preset. Este atraso só é usado se a câmera não informar a "
                                 "posição do zoom (reserva).")
        form.addRow("Atraso das cores no preset (reserva):", self.sp_delay)

        btns = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Save |
                                          QtWidgets.QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        form.addRow(btns)

    def valores(self):
        return {
            "ip": self.ed_ip.text().strip(),
            "rtsp_url": self.ed_rtsp.text().strip(),
            "protocolo": self.cb_proto.currentText(),
            "visca_porta": self.sp_visca.value(),
            "http_porta": self.sp_http.value(),
            "inverter_pan": "true" if self.ck_inv_pan.isChecked() else "false",
            "inverter_tilt": "true" if self.ck_inv_tilt.isChecked() else "false",
            "wb_var_codigo": self.sp_wbvar.value(),
            "largura_processamento": self.cb_res.currentText().split()[0],
            "delay_cores": self.sp_delay.value(),
        }


# ============================================================================
# Janela principal
# ============================================================================
class Janela(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Rastreador PTZ - Profissional")
        self.setObjectName("JanelaPTZ")
        self.cfg = carregar_config()
        self.estado = carregar_estado(self.cfg)
        # aplica a conexão configurada no menu do app (por cima do config.ini)
        self._aplicar_overrides_conexao(self.estado.get("conexao", {}))

        # parâmetros
        self.proc_w = self.cfg.getint("rastreamento", "largura_processamento", fallback=1280)
        self.det_w = self.cfg.getint("rastreamento", "deteccao_largura", fallback=736)
        self.det_conf = self.cfg.getfloat("rastreamento", "deteccao_confianca", fallback=0.35)
        self.zona_x = self.cfg.getfloat("rastreamento", "zona_morta_x", fallback=0.06)
        self.zona_y = self.cfg.getfloat("rastreamento", "zona_morta_y", fallback=0.06)
        self.ganho_pan = self.cfg.getfloat("rastreamento", "ganho_pan", fallback=12.0)
        self.ganho_tilt = self.cfg.getfloat("rastreamento", "ganho_tilt", fallback=10.0)
        self.vmax_pan = self.cfg.getint("rastreamento", "vel_max_pan", fallback=9)
        self.vmax_tilt = self.cfg.getint("rastreamento", "vel_max_tilt", fallback=7)
        # Velocidade de seguir: quanto a câmera ACELERA quando o pastor se aproxima
        # da borda (pra não deixar ele escapar). 1.0 = sem turbo; 2.5 = corre bem
        # mais rápido na lateral. No centro fica sempre firme (turbo = 1).
        self.seguir_vel = self.cfg.getfloat("rastreamento", "velocidade_seguir", fallback=2.0)
        self.mira_v = self.cfg.getfloat("rastreamento", "mira_vertical", fallback=0.30)
        self.tela_x = self.cfg.getfloat("rastreamento", "posicao_tela_x", fallback=0.50)
        self.tela_y = self.cfg.getfloat("rastreamento", "posicao_tela_y", fallback=0.38)
        self.zoom_auto = self.cfg.getboolean("rastreamento", "zoom_automatico", fallback=False)
        self.zoom_alvo = self.cfg.getfloat("rastreamento", "zoom_alvo_altura", fallback=0.70)
        self.timeout_perda = self.cfg.getfloat("rastreamento", "timeout_perda", fallback=3.0)
        self.raio_reid = self.cfg.getfloat("rastreamento", "raio_reidentificacao", fallback=0.12)
        self.preset_delay_cores = self.cfg.getfloat("presets", "delay_cores", fallback=2.0)
        self.margem = self.cfg.getfloat("rastreamento", "margem_seguranca", fallback=0.22)
        self.margem_vert = self.cfg.getfloat("rastreamento", "margem_vertical", fallback=0.32)
        self.parar_em = self.cfg.getfloat("rastreamento", "parar_em", fallback=0.05)
        # FIRMEZA VERTICAL: ignora de vez pequenos tremores de cima/baixo (respirar,
        # balançar a cabeça ao falar) antes mesmo de decidir se corrige — não é só
        # "não iniciar o movimento", é nem CONSIDERAR o tremor pequeno como alvo novo.
        self.firmeza_vert = self.cfg.getfloat("rastreamento", "firmeza_vertical", fallback=0.03)
        self.suav = self.cfg.getfloat("rastreamento", "suavizacao", fallback=0.15)
        self.acel = self.cfg.getfloat("rastreamento", "aceleracao", fallback=1.0)
        self.modelo_nome = self.cfg.get("rastreamento", "modelo", fallback="yolov8n.pt")

        self.vman_pan = self.cfg.getint("manual", "vel_pan", fallback=10)
        self.vman_tilt = self.cfg.getint("manual", "vel_tilt", fallback=8)
        self.vman_zoom = self.cfg.getint("manual", "vel_zoom", fallback=3)
        self.escala_mult = 1.0

        # câmera e vídeo
        self.camera = ControladorCamera(self.cfg)
        self.video = LeitorVideo(self.cfg.get("camera", "rtsp_url")).iniciar()

        # estado do rastreamento
        self.caixas = []
        self._aims = {}              # id -> (aim_x, aim_y) vindo da pose (ombros/cabeça)
        self.alvo_id = None          # só para desenhar (não confiamos nele p/ seguir)
        self.alvo_box = None         # caixa rastreada do alvo (x1,y1,x2,y2)
        self.alvo_aim = None         # ponto de mira do alvo (ombros/cabeça)
        self.aim_confiavel = False   # False = mira em modo "reserva" (pose perdida)
        self.alvo_vel = (0.0, 0.0)   # velocidade do centro (previsão)
        self.pausado = False
        self.ultimo_visto = 0.0
        # suavização / histerese do movimento
        self.sx = None
        self.sy = None
        self.mov_x = False
        self.mov_y = False
        self.vp = 0.0
        self.vt = 0.0
        self._mov_ativo = False      # estava se movendo no quadro anterior?
        self._settle_ate = 0.0       # "descanso" após parar (evita oscilar com atraso do vídeo)
        self.sh = None               # altura suavizada do alvo (para o zoom automático)
        self._zoom_ativo = False     # está ajustando o zoom agora? (histerese)
        self._foco_pulsando = False  # trava enquanto um pulso de foco está ativo
        self.foco_passo = 30         # ms por toque no ajuste fino do foco
        self._recall_ate = 0.0       # não manda comando enquanto a câmera vai pro preset
        # antecipação (quando corre pro lado, a câmera "chumba" na frente)
        self.antecipar = self.cfg.getboolean("rastreamento", "antecipar", fallback=False)
        self.antecipar_ganho = self.cfg.getfloat("rastreamento", "antecipar_ganho", fallback=8.0)
        self.W = self.proc_w
        self.H = int(self.proc_w * 9 / 16)
        self.frame_atual = None
        self.teclas = set()
        # últimas posições de íris/shutter lidas da câmera (para salvar no preset)
        self.ultimo_iris_pos = None
        self.ultimo_shutter_pos = None
        self.modo_txt = "SEM ALVO"
        self.fps = 0.0
        self._fps_t = time.time()

        # gamepad
        gp_on = self.cfg.getboolean("gamepad", "ativar", fallback=True)
        self.gamepad = Gamepad(self.cfg, ativar=gp_on)

        # aplica ajustes de rastreamento SALVOS (por cima dos padrões do config.ini)
        r = self.estado.get("rastreamento", {})
        for attr in ("escala_mult", "tela_x", "tela_y", "margem", "margem_vert", "suav",
                     "firmeza_vert", "antecipar_ganho", "seguir_vel",
                     "vman_pan", "vman_tilt", "vman_zoom"):
            if attr in r:
                setattr(self, attr, r[attr])
        if "zoom_auto" in r:
            self.zoom_auto = bool(r["zoom_auto"])
        if "antecipar" in r:
            self.antecipar = bool(r["antecipar"])

        # timer que salva os ajustes de rastreamento (com um pequeno atraso p/ não
        # gravar o arquivo a cada micro-movimento do slider)
        self._timer_salvar = QtCore.QTimer(self)
        self._timer_salvar.setSingleShot(True)
        self._timer_salvar.timeout.connect(self._salvar_rastreamento)

        # timer que relê os valores da câmera após ajustar shutter/íris
        self._timer_reler = QtCore.QTimer(self)
        self._timer_reler.setSingleShot(True)
        self._timer_reler.timeout.connect(self._puxar_da_camera)

        # envio de cores em segundo plano (HTTP não pode travar a tela)
        self._cor_pendente = {}
        self._timer_cor = QtCore.QTimer(self)
        self._timer_cor.setSingleShot(True)
        self._timer_cor.timeout.connect(self._flush_cores)

        # interface
        self._montar_ui()

        # detector
        self.detector = Detector(self.video.ler, self.proc_w, self.det_w,
                                 self.modelo_nome, self.det_conf)
        self.detector.resultado.connect(self._novas_caixas)
        self.detector.aviso.connect(self.statusBar().showMessage)
        self.detector.start()

        # ao abrir, PUXA as configurações que já estão na câmera (não empurra defaults)
        self._leitor_cfg = None
        QtCore.QTimer.singleShot(1500, self._puxar_da_camera)

        # timers
        self.t_render = QtCore.QTimer(self)
        self.t_render.timeout.connect(self._render)
        self.t_render.start(33)          # ~30 fps de tela
        self.t_ctrl = QtCore.QTimer(self)
        self.t_ctrl.timeout.connect(self._controle)
        self.t_ctrl.start(30)            # ~33 Hz de controle

    # ---- construção da interface -----------------------------------------
    def _montar_ui(self):
        self.video_lbl = VideoLabel()
        self.video_lbl.clicado.connect(self._clique_video)
        self.video_lbl.foco_aqui.connect(self._focar_aqui)
        self.setCentralWidget(self.video_lbl)

        self.setDockNestingEnabled(True)
        self.setDockOptions(QtWidgets.QMainWindow.AllowNestedDocks |
                            QtWidgets.QMainWindow.AllowTabbedDocks |
                            QtWidgets.QMainWindow.AnimatedDocks)
        self._docks = {}
        self._docks_ctrl = set()   # títulos dos painéis que estão na 2ª tela

        # Segunda janela ("tela de controles") — um container próprio onde você
        # pode encaixar VÁRIOS painéis juntos e jogar em outro monitor.
        self.win_ctrl = QtWidgets.QMainWindow()
        self.win_ctrl.setWindowTitle("Controles PTZ — 2ª tela")
        self.win_ctrl.setObjectName("JanelaControles")
        self.win_ctrl.setDockNestingEnabled(True)
        self.win_ctrl.setDockOptions(QtWidgets.QMainWindow.AllowNestedDocks |
                                     QtWidgets.QMainWindow.AllowTabbedDocks |
                                     QtWidgets.QMainWindow.AnimatedDocks)
        # widget central 0x0: assim os painéis preenchem a janela INTEIRA
        # (não sobra aquela área vazia embaixo que não aceita nada).
        _ph = QtWidgets.QWidget()
        _ph.setMaximumSize(0, 0)
        self.win_ctrl.setCentralWidget(_ph)
        self.win_ctrl.resize(440, 820)

        R = QtCore.Qt.RightDockWidgetArea
        B = QtCore.Qt.BottomDockWidgetArea
        specs = [
            ("Rastreamento", self._grupo_rastreamento(), R),
            ("Ponto central", self._grupo_ponto_central(), R),
            ("Movimento", self._grupo_movimento(), R),
            ("Antecipação", self._grupo_antecipacao(), R),
            ("Velocidade manual", self._grupo_velocidade(), R),
            ("Foco", self._grupo_foco(), B),
            ("Presets", self._grupo_presets(), B),
            ("Cores / Imagem", self._grupo_cores(), B),
            ("Exposição / White Balance", self._grupo_exposicao(), B),
            ("Menu da câmera (OSD)", self._grupo_osd(), B),
        ]
        for titulo, w, area in specs:
            self._add_dock(titulo, w, area)

        self._montar_menu()
        self.statusBar().showMessage("Carregando IA...")
        self.resize(1500, 860)
        self._aplicar_tema()
        self._restaurar_layout()

        # Botões e caixas de seleção NÃO capturam o teclado: assim as teclas
        # (Espaço=pausa, N, C, 0-9, WASD...) sempre funcionam, mesmo depois de
        # clicar num botão (antes o Espaço reapertava o último botão clicado).
        for raiz in (self, self.win_ctrl):
            for wdg in raiz.findChildren(QtWidgets.QPushButton):
                wdg.setFocusPolicy(QtCore.Qt.NoFocus)
            for wdg in raiz.findChildren(QtWidgets.QCheckBox):
                wdg.setFocusPolicy(QtCore.Qt.NoFocus)
        self.video_lbl.setFocusPolicy(QtCore.Qt.StrongFocus)
        self.video_lbl.setFocus()
        self._montar_atalhos()
        # WASD/Q/E/Espaço/N/C também funcionam na 2ª tela: keyPressEvent só
        # dispara na janela que tem o foco, e a 2ª tela é uma QMainWindow
        # separada — sem isto, o teclado só mexia a câmera com a tela
        # principal em primeiro plano. Filtro de aplicação inteiro (não só
        # a 2ª tela) para pegar também os painéis quando movidos pra lá.
        QtWidgets.QApplication.instance().installEventFilter(self)

    def _montar_atalhos(self):
        # Ctrl + 1..9 chama os presets — atalho de APLICAÇÃO (funciona em qualquer
        # janela/painel, inclusive na 2ª tela, e mesmo com foco num slider).
        self._atalhos = []
        for n in range(1, 10):
            sc = QtGui.QShortcut(QtGui.QKeySequence("Ctrl+%d" % n), self)
            sc.setContext(QtCore.Qt.ApplicationShortcut)
            sc.activated.connect(lambda n=n: self._chamar_preset(n))
            self._atalhos.append(sc)

    def _salvar_layout(self):
        try:
            b64 = lambda ba: base64.b64encode(bytes(ba)).decode("ascii")
            self.estado["layout"] = {
                "geometry": b64(self.saveGeometry()),
                "state": b64(self.saveState()),
                "ctrl_geometry": b64(self.win_ctrl.saveGeometry()),
                "ctrl_state": b64(self.win_ctrl.saveState()),
                "ctrl_docks": list(self._docks_ctrl),
                "ctrl_visivel": self.win_ctrl.isVisible(),
            }
            salvar_estado(self.estado)
        except Exception as e:
            print("Aviso: não consegui salvar o layout:", e)

    def _restaurar_layout(self):
        lay = self.estado.get("layout", {})
        try:
            dec = lambda s: QtCore.QByteArray(base64.b64decode(s))
            # move p/ a 2ª tela os painéis que estavam nela (antes de restaurar estados)
            for t in lay.get("ctrl_docks", []):
                if t in self._docks:
                    self._para_ctrl(t)
            if lay.get("geometry"):
                self.restoreGeometry(dec(lay["geometry"]))
            if lay.get("state"):
                self.restoreState(dec(lay["state"]))
            if lay.get("ctrl_geometry"):
                self.win_ctrl.restoreGeometry(dec(lay["ctrl_geometry"]))
            if lay.get("ctrl_state"):
                self.win_ctrl.restoreState(dec(lay["ctrl_state"]))
            if lay.get("ctrl_visivel") and self._docks_ctrl:
                self.win_ctrl.show()
        except Exception as e:
            print("Aviso: não consegui restaurar o layout:", e)

    def _add_dock(self, titulo, widget, area):
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(widget)
        dock = QtWidgets.QDockWidget(titulo, self)
        dock.setObjectName(titulo)
        dock.setWidget(scroll)
        dock.setFeatures(QtWidgets.QDockWidget.DockWidgetMovable |
                         QtWidgets.QDockWidget.DockWidgetFloatable |
                         QtWidgets.QDockWidget.DockWidgetClosable)
        self.addDockWidget(area, dock)
        self._docks[titulo] = dock
        return dock

    def _montar_menu(self):
        mb = self.menuBar()
        m_ptz = mb.addMenu("PTZ")
        m_ptz.addAction("Configurações de conexão (IP, portas)...", self._abrir_config_conexao)
        m_ptz.addAction("Puxar configurações da câmera", self._puxar_da_camera)
        m_ptz.addAction("Abrir painel web da câmera (navegador)", self._abrir_web_camera)
        m_ptz.addSeparator()
        m_ptz.addAction("Sair", self.close)

        m_pan = mb.addMenu("Painéis")
        for titulo, dock in self._docks.items():
            m_pan.addAction(dock.toggleViewAction())

        # menu "Janela": tela de controles secundária (2º monitor)
        m_win = mb.addMenu("Janela")
        m_win.addAction("Abrir 2ª tela de controles", lambda: (self.win_ctrl.show(),
                        self.win_ctrl.raise_()))
        m_win.addAction("Mover TODOS os controles para a 2ª tela", self._todos_para_ctrl)
        sub = m_win.addMenu("Mover um painel para a 2ª tela")
        for titulo in self._docks:
            sub.addAction(titulo, lambda t=titulo: self._para_ctrl(t))
        m_win.addSeparator()
        m_win.addAction("Trazer todos de volta para a janela principal", self._todos_para_principal)

    def _para_ctrl(self, titulo):
        """Move um painel para a 2ª tela (container de controles)."""
        d = self._docks.get(titulo)
        if d is None:
            return
        self.removeDockWidget(d)
        d.setFloating(False)
        self.win_ctrl.addDockWidget(QtCore.Qt.TopDockWidgetArea, d)
        d.show()
        self._docks_ctrl.add(titulo)
        self.win_ctrl.show()
        self.win_ctrl.raise_()

    def _para_principal(self, titulo):
        d = self._docks.get(titulo)
        if d is None:
            return
        self.win_ctrl.removeDockWidget(d)
        d.setFloating(False)
        self.addDockWidget(QtCore.Qt.RightDockWidgetArea, d)
        d.show()
        self._docks_ctrl.discard(titulo)

    def _todos_para_ctrl(self):
        for t in list(self._docks):
            self._para_ctrl(t)

    def _todos_para_principal(self):
        for t in list(self._docks):
            self._para_principal(t)

    def _abrir_web_camera(self):
        import webbrowser
        webbrowser.open("http://%s/" % self.camera.ip)
        self.statusBar().showMessage("Abrindo o painel web da câmera no navegador...", 4000)

    def _aplicar_overrides_conexao(self, v):
        """Grava os valores da tela de conexão por cima do config.ini em memória
        (o arquivo config.ini com os comentários não é alterado)."""
        def _set(sec, key, val):
            if not self.cfg.has_section(sec):
                self.cfg.add_section(sec)
            self.cfg.set(sec, key, str(val))
        mapa = {
            "ip": ("camera", "ip"), "rtsp_url": ("camera", "rtsp_url"),
            "protocolo": ("controle", "protocolo"),
            "visca_porta": ("controle", "visca_porta"),
            "http_porta": ("controle", "http_porta"),
            "inverter_pan": ("controle", "inverter_pan"),
            "inverter_tilt": ("controle", "inverter_tilt"),
            "wb_var_codigo": ("cores", "wb_var_codigo"),
            "largura_processamento": ("rastreamento", "largura_processamento"),
            "delay_cores": ("presets", "delay_cores"),
        }
        for chave, (sec, key) in mapa.items():
            if chave in v and str(v[chave]) != "":
                _set(sec, key, v[chave])

    def _abrir_config_conexao(self):
        dlg = DialogoConexao(self, self.cfg)
        if dlg.exec():
            vals = dlg.valores()
            self.estado["conexao"] = vals
            salvar_estado(self.estado)
            self._aplicar_overrides_conexao(vals)
            self.preset_delay_cores = self.cfg.getfloat("presets", "delay_cores", fallback=2.0)
            self._reconectar()

    def _reconectar(self):
        """Recria a conexão com a câmera e o vídeo usando os novos valores."""
        try:
            try:
                self.camera.fechar()
            except Exception:
                pass
            self.camera = ControladorCamera(self.cfg)
            try:
                self.video.parar()
            except Exception:
                pass
            self.video = LeitorVideo(self.cfg.get("camera", "rtsp_url")).iniciar()
            self.proc_w = self.cfg.getint("rastreamento", "largura_processamento", fallback=1280)
            if hasattr(self, "detector"):
                self.detector.get_frame = self.video.ler
                self.detector.proc_w = self.proc_w
            self.statusBar().showMessage("Conexão atualizada e aplicada.", 5000)
        except Exception as e:
            self.statusBar().showMessage("Erro ao reconectar (tente reabrir o app): %s" % e, 8000)

    def _titulo(self, _txt=""):
        # o título agora aparece na barra do painel (dock); aqui é só o container
        return QtWidgets.QWidget()

    def _slider(self, mini, maxi, val, cb, passo=1):
        s = SliderSemRoda(QtCore.Qt.Horizontal)
        s.setFocusPolicy(QtCore.Qt.StrongFocus)  # só pega foco por clique/tab, não pela roda
        s.setMinimum(mini)
        s.setMaximum(maxi)
        s.setValue(val)
        s.setSingleStep(passo)
        s.valueChanged.connect(cb)
        return s

    def _grupo_rastreamento(self):
        g = self._titulo("Rastreamento")
        v = QtWidgets.QVBoxLayout(g)
        self.lbl_status = QtWidgets.QLabel("—")
        self.lbl_status.setStyleSheet("font-size:16px;font-weight:bold;")
        v.addWidget(self.lbl_status)

        h = QtWidgets.QHBoxLayout()
        b1 = QtWidgets.QPushButton("Pausar/Retomar")
        b1.clicked.connect(self._toggle_pausa)
        b2 = QtWidgets.QPushButton("Próxima pessoa")
        b2.clicked.connect(self._proxima_pessoa)
        h.addWidget(b1)
        h.addWidget(b2)
        v.addLayout(h)
        b3 = QtWidgets.QPushButton("Cancelar alvo (parar de seguir)")
        b3.clicked.connect(self._cancelar_alvo)
        v.addWidget(b3)

        v.addWidget(QtWidgets.QLabel("Sensibilidade (velocidade de reação)"))
        self.sld_sens = self._slider(3, 30, int(self.escala_mult * 10),
                                     lambda x: self._set_rastr("escala_mult", x / 10.0))
        v.addWidget(self.sld_sens)

        v.addWidget(QtWidgets.QLabel("Velocidade de seguir (acelera na lateral p/ não perder)"))
        self.sld_seguir = self._slider(10, 30, int(self.seguir_vel * 10),
                                       lambda x: self._set_rastr("seguir_vel", x / 10.0))
        self.sld_seguir.setToolTip("No centro a câmera fica firme; quanto mais o pastor se "
                                   "aproxima da borda, mais RÁPIDO ela corre atrás. Maior = "
                                   "acompanha quem anda rápido sem deixar sair do quadro.")
        v.addWidget(self.sld_seguir)

        v.addWidget(QtWidgets.QLabel("Enquadramento (zoom)"))
        self.cmb_enq = ComboSemRoda()
        self.cmb_enq.setFocusPolicy(QtCore.Qt.StrongFocus)
        self.cmb_enq.addItems(["Manual (eu ajusto o zoom)", "Corpo inteiro",
                               "Meio corpo", "Primeiro plano (rosto)"])
        self.cmb_enq.currentIndexChanged.connect(self._set_enquadramento)
        v.addWidget(self.cmb_enq)
        self.chk_zoom = QtWidgets.QCheckBox("Zoom automático")
        self.chk_zoom.setChecked(self.zoom_auto)
        self.chk_zoom.stateChanged.connect(
            lambda s: self._set_rastr("zoom_auto", bool(s)))
        v.addWidget(self.chk_zoom)
        return g

    def _set_enquadramento(self, idx):
        # Manual = zoom automático desligado (o operador ajusta e fica).
        # Os demais ligam o auto-zoom e definem o tamanho-alvo da pessoa na tela.
        if idx == 0:
            self.chk_zoom.setChecked(False)
            return
        alvos = {1: (0.72, 0.44), 2: (0.95, 0.40), 3: (1.25, 0.35)}
        z, ty = alvos.get(idx, (0.72, 0.44))
        self.zoom_alvo = z
        self.tela_y = ty
        self.sld_ty.blockSignals(True)
        self.sld_ty.setValue(int(ty * 100))
        self.sld_ty.blockSignals(False)
        self.chk_zoom.setChecked(True)
        self._set_rastr("zoom_auto", True)

    def _grupo_ponto_central(self):
        g = self._titulo("Ponto central (enquadramento)")
        v = QtWidgets.QVBoxLayout(g)
        v.addWidget(QtWidgets.QLabel("Altura do rosto na tela (sobe = tira vão de cima)"))
        self.sld_ty = self._slider(15, 70, int(self.tela_y * 100),
                                   lambda x: self._set_rastr("tela_y", x / 100.0))
        v.addWidget(self.sld_ty)
        v.addWidget(QtWidgets.QLabel("Posição horizontal (esquerda ⟷ direita)"))
        self.sld_tx = self._slider(20, 80, int(self.tela_x * 100),
                                   lambda x: self._set_rastr("tela_x", x / 100.0))
        v.addWidget(self.sld_tx)
        v.addWidget(QtWidgets.QLabel("Margem vertical (maior = não corta a cabeça ao abaixar)"))
        self.sld_mvert = self._slider(8, 60, int(self.margem_vert * 100),
                                      lambda x: self._set_rastr("margem_vert", x / 100.0))
        v.addWidget(self.sld_mvert)
        v.addWidget(QtWidgets.QLabel("Firmeza vertical (maior = ignora mais tremor de cima/baixo)"))
        self.sld_firmeza = self._slider(1, 12, int(self.firmeza_vert * 100),
                                        lambda x: self._set_rastr("firmeza_vert", x / 100.0))
        self.sld_firmeza.setToolTip("Ignora pequenos tremores verticais (respirar, balançar a "
                                    "cabeça ao falar) antes mesmo de considerar corrigir. Se "
                                    "ainda estiver corrigindo por pouca coisa, aumente aqui.")
        v.addWidget(self.sld_firmeza)
        return g

    def _grupo_movimento(self):
        g = self._titulo("Movimento (suavidade)")
        v = QtWidgets.QVBoxLayout(g)
        v.addWidget(QtWidgets.QLabel("Margem de segurança (maior = câmera mais FIRME/parada)"))
        self.sld_margem = self._slider(5, 55, int(self.margem * 100),
                                       lambda x: self._set_rastr("margem", x / 100.0))
        v.addWidget(self.sld_margem)
        v.addWidget(QtWidgets.QLabel("Suavidade (maior = mais macio e estável)"))
        # slider 1..20; suavização = de 0.30 (pouco suave) a 0.05 (bem suave)
        ini = int(round((0.30 - self.suav) / 0.25 * 19)) + 1
        self.sld_suav = self._slider(1, 20, max(1, min(20, ini)),
                                     lambda x: self._set_rastr("suav", 0.30 - (x - 1) / 19.0 * 0.25))
        v.addWidget(self.sld_suav)
        return g

    def _grupo_antecipacao(self):
        g = self._titulo("Antecipação")
        v = QtWidgets.QVBoxLayout(g)
        self.chk_antecipar = QtWidgets.QCheckBox("Antecipar movimento (quando corre pro lado)")
        self.chk_antecipar.setToolTip("Quando o pastor sai andando/correndo para um lado, a "
                                      "câmera 'chumba' na frente dele e continua indo naquele "
                                      "lado, em vez de ficar sempre atrás.")
        self.chk_antecipar.setChecked(self.antecipar)
        self.chk_antecipar.stateChanged.connect(
            lambda s: self._set_rastr("antecipar", bool(s)))
        v.addWidget(self.chk_antecipar)
        v.addWidget(QtWidgets.QLabel("Quanto antecipar (maior = chumba mais na frente)"))
        self.sld_antec = self._slider(0, 30, int(self.antecipar_ganho),
                                      lambda x: self._set_rastr("antecipar_ganho", float(x)))
        v.addWidget(self.sld_antec)
        return g

    def _grupo_foco(self):
        g = self._titulo("Foco")
        v = QtWidgets.QVBoxLayout(g)

        bfix = QtWidgets.QPushButton("🎯  FOCAR NA PESSOA E TRAVAR")
        bfix.setStyleSheet("font-weight:bold;padding:9px;")
        bfix.setToolTip("Foca na pessoa enquadrada e trava — não fica caçando o telão. "
                        "Dica: clique com o botão DIREITO no vídeo faz o mesmo.")
        bfix.clicked.connect(self._focar_travar)
        v.addWidget(bfix)
        v.addWidget(QtWidgets.QLabel("(ou clique com o botão DIREITO no vídeo)"))

        self.chk_foco_auto = QtWidgets.QCheckBox("Foco automático contínuo")
        self.chk_foco_auto.setToolTip("Ligado, a câmera refoca sozinha (pode caçar o telão). "
                                      "Desligado, o foco fica travado onde você deixou.")
        self.chk_foco_auto.setChecked(False)
        self.chk_foco_auto.stateChanged.connect(
            lambda s: self.camera.set_foco_auto(bool(s)))
        v.addWidget(self.chk_foco_auto)

        # --- Ajuste FINO do foco: cada clique = um PULSO curtíssimo no motor ---
        # (mais fino que posição absoluta; a delicadeza é o tempo do pulso em ms)
        v.addWidget(QtWidgets.QLabel("Ajuste fino do foco (toques leves):"))
        h = QtWidgets.QHBoxLayout()
        # sinal: negativo = perto, positivo = longe ; fator do tempo do pulso
        for txt, fator in [("◀◀", -3), ("◀ perto", -1), ("longe ▶", 1), ("▶▶", 3)]:
            b = QtWidgets.QPushButton(txt)
            b.setAutoRepeat(True)          # segurar = vários pulsos seguidos (devagar)
            b.setAutoRepeatDelay(400)
            b.setAutoRepeatInterval(220)
            b.clicked.connect(lambda _=0, f=fator: self._passo_foco(f))
            h.addWidget(b)
        v.addLayout(h)

        v.addWidget(QtWidgets.QLabel("Delicadeza (ms por toque — menor = mais fino)"))
        self.sld_foco_passo = self._slider(12, 120, 30,
                                           lambda x: setattr(self, "foco_passo", x))
        self.sld_foco_passo.setToolTip("Cada toque liga o foco na velocidade mais lenta por "
                                       "este tempo. Comece em 30; se ainda mexer demais, "
                                       "diminua para 12-20.")
        v.addWidget(self.sld_foco_passo)
        return g

    def _garantir_foco_manual(self):
        if self.chk_foco_auto.isChecked():
            self.chk_foco_auto.setChecked(False)   # dispara set_foco_auto(False)
        else:
            self.camera.set_foco_auto(False)

    def _passo_foco(self, fator):
        """Pulso curtíssimo no motor de foco = ajuste realmente fino e repetível.
        fator<0 = perto, fator>0 = longe; |fator| multiplica o tempo do pulso."""
        if getattr(self, "_foco_pulsando", False):
            return  # ignora novos toques enquanto um pulso ainda está em andamento
        self._garantir_foco_manual()
        self._foco_pulsando = True
        dur = max(8, int(getattr(self, "foco_passo", 30) * abs(fator)))   # ms
        if fator < 0:
            self.camera.foco_perto(True, 0)     # velocidade 0 = a mais lenta
        else:
            self.camera.foco_longe(True, 0)
        QtCore.QTimer.singleShot(dur, self._parar_foco_pulso)

    def _parar_foco_pulso(self):
        self.camera.foco_parar()                # STOP enviado 3x (não dispara)
        self._foco_pulsando = False

    def _focar_travar(self):
        """Foca uma vez no centro e trava (não caça o telão)."""
        self.chk_foco_auto.setChecked(False)
        self.camera.focar_e_travar()
        self.statusBar().showMessage("Focado no centro e travado.", 3000)

    def _focar_aqui(self, x, y):
        """Botão direito no vídeo: foca e trava (na região enquadrada)."""
        self.chk_foco_auto.setChecked(False)
        self.camera.focar_e_travar()
        self.statusBar().showMessage("Focando ali e travando...", 3000)

    def _foco_gamepad(self, x):
        """Foco contínuo pelo analógico direito (X). Só envia quando muda."""
        novo = 1 if x > 0.3 else (-1 if x < -0.3 else 0)
        if novo == getattr(self, "_foco_gp_estado", 0):
            return
        self._foco_gp_estado = novo
        if novo == 0:
            self.camera.foco_perto(False)   # parar
            return
        if self.chk_foco_auto.isChecked():
            self.chk_foco_auto.setChecked(False)
        vel = self.sld_foco_vel.value() if hasattr(self, "sld_foco_vel") else 2
        if novo > 0:
            self.camera.foco_longe(True, vel)
        else:
            self.camera.foco_perto(True, vel)

    def _garantir_exposicao_manual(self):
        """Íris/Shutter só respondem fora do modo Automático. Se estiver em Auto,
        muda para Manual (o comando de exposição é enviado pelo _set_exp)."""
        if self.cmb_exp.currentIndex() == 0:      # 0 = Automática
            self.cmb_exp.setCurrentIndex(1)       # 1 = Manual

    def _ajustar_iris(self, direcao):
        """Abre/fecha a íris um passo, garante Manual e mostra o valor REAL."""
        self._garantir_exposicao_manual()
        if direcao > 0:
            self.camera.iris_mais()
        else:
            self.camera.iris_menos()
        iris, _sh = self.camera.ler_pos_iris_shutter()
        if iris is not None:
            self.ultimo_iris_pos = iris
        elif self.ultimo_iris_pos is not None:
            self.ultimo_iris_pos = max(IRIS_MIN, min(IRIS_MAX, self.ultimo_iris_pos + direcao))
        self.lbl_iris.setText(_rotulo_valor(IRIS_STR, self.ultimo_iris_pos)
                              if self.ultimo_iris_pos is not None else "—")

    def _ajustar_shutter(self, direcao):
        """Shutter mais rápido/lento, garante Manual e mostra o valor REAL."""
        self._garantir_exposicao_manual()
        if direcao > 0:
            self.camera.shutter_mais()
        else:
            self.camera.shutter_menos()
        _ir, shut = self.camera.ler_pos_iris_shutter()
        if shut is not None:
            self.ultimo_shutter_pos = shut
        elif self.ultimo_shutter_pos is not None:
            self.ultimo_shutter_pos = max(SHUTTER_MIN, min(SHUTTER_MAX, self.ultimo_shutter_pos + direcao))
        self.lbl_shutter.setText(_rotulo_valor(SHUTTER_STR, self.ultimo_shutter_pos)
                                 if self.ultimo_shutter_pos is not None else "—")

    def _grupo_velocidade(self):
        g = self._titulo("Velocidade do controle manual")
        v = QtWidgets.QVBoxLayout(g)
        v.addWidget(QtWidgets.QLabel("Giro (pan)"))
        v.addWidget(self._slider(1, 24, self.vman_pan,
                                 lambda x: self._set_rastr("vman_pan", x)))
        v.addWidget(QtWidgets.QLabel("Inclinação (tilt)"))
        v.addWidget(self._slider(1, 20, self.vman_tilt,
                                 lambda x: self._set_rastr("vman_tilt", x)))
        v.addWidget(QtWidgets.QLabel("Zoom"))
        v.addWidget(self._slider(1, 7, self.vman_zoom,
                                 lambda x: self._set_rastr("vman_zoom", x)))
        return g

    def _grupo_presets(self):
        g = self._titulo("Presets (posições salvas na câmera)")
        v = QtWidgets.QVBoxLayout(g)
        dica = QtWidgets.QLabel("Atalho: Ctrl+1 … Ctrl+9 chama o preset (funciona em qualquer tela)")
        dica.setStyleSheet("color:#8a8f98;")
        v.addWidget(dica)
        self.preset_edits = {}
        for i in range(1, 10):
            h = QtWidgets.QHBoxLayout()
            e = QtWidgets.QLineEdit(self.estado["presets"].get(str(i), "Preset %d" % i))
            e.editingFinished.connect(lambda i=i: self._renomear_preset(i))
            self.preset_edits[i] = e
            bc = QtWidgets.QPushButton("Ir")
            bc.setFixedWidth(40)
            bc.clicked.connect(lambda _=0, i=i: self._chamar_preset(i))
            bs = QtWidgets.QPushButton("Salvar")
            bs.setFixedWidth(60)
            bs.clicked.connect(lambda _=0, i=i: self._salvar_preset(i))
            h.addWidget(QtWidgets.QLabel(str(i)))
            h.addWidget(e)
            h.addWidget(bc)
            h.addWidget(bs)
            v.addLayout(h)
        return g

    def _grupo_cores(self):
        # valores nativos da câmera (0-14), lidos/ajustados por HTTP
        g = self._titulo("Cores / Imagem")
        v = QtWidgets.QVBoxLayout(g)
        c = self.estado["cores"]
        MAX = 14
        v.addWidget(QtWidgets.QLabel("Saturação"))
        self.sld_sat = self._slider(0, MAX, min(MAX, c["saturacao"]),
                                    lambda x: self._cor("saturacao", x, self.camera.set_saturacao))
        v.addWidget(self.sld_sat)
        v.addWidget(QtWidgets.QLabel("Contraste"))
        self.sld_con = self._slider(0, MAX, min(MAX, c.get("contraste", 7)),
                                    lambda x: self._cor("contraste", x, self.camera.set_contraste))
        v.addWidget(self.sld_con)
        v.addWidget(QtWidgets.QLabel("Brilho"))
        self.sld_bri = self._slider(0, MAX, min(MAX, c["brilho"]),
                                    lambda x: self._cor("brilho", x, self.camera.set_brilho))
        v.addWidget(self.sld_bri)
        v.addWidget(QtWidgets.QLabel("Matiz (tonalidade)"))
        self.sld_mat = self._slider(0, MAX, min(MAX, c["matiz"]),
                                    lambda x: self._cor("matiz", x, self.camera.set_matiz))
        v.addWidget(self.sld_mat)
        v.addWidget(QtWidgets.QLabel("Nitidez"))
        self.sld_nit = self._slider(0, MAX, min(MAX, c["nitidez"]),
                                    lambda x: self._cor("nitidez", x, self.camera.set_nitidez))
        v.addWidget(self.sld_nit)

        # espelhamento e correção de lente (via HTTP, confirmados na câmera)
        self.chk_flip = QtWidgets.QCheckBox("Espelhar vertical (flip)")
        self.chk_flip.stateChanged.connect(lambda s: self._osd(lambda: self.camera.set_flip(bool(s))))
        v.addWidget(self.chk_flip)
        self.chk_mirror = QtWidgets.QCheckBox("Espelhar horizontal (mirror)")
        self.chk_mirror.stateChanged.connect(lambda s: self._osd(lambda: self.camera.set_mirror(bool(s))))
        v.addWidget(self.chk_mirror)
        self.chk_ldc = QtWidgets.QCheckBox("Correção de distorção da lente (LDC)")
        self.chk_ldc.stateChanged.connect(lambda s: self._osd(lambda: self.camera.set_ldc(bool(s))))
        v.addWidget(self.chk_ldc)
        return g

    def _grupo_osd(self):
        g = self._titulo("Menu da câmera (OSD)")
        v = QtWidgets.QVBoxLayout(g)
        v.addWidget(QtWidgets.QLabel("Abre o menu interno da câmera sobre o vídeo\n"
                                     "(o mesmo do controle físico). Navegue com as setas."))
        bm = QtWidgets.QPushButton("☰  Abrir / Fechar Menu")
        bm.clicked.connect(lambda: self._osd(self.camera.osd_menu))
        v.addWidget(bm)

        # setas em cruz
        grade = QtWidgets.QGridLayout()
        bcima = QtWidgets.QPushButton("▲")
        besq = QtWidgets.QPushButton("◀")
        bok = QtWidgets.QPushButton("OK")
        bdir = QtWidgets.QPushButton("▶")
        bbaixo = QtWidgets.QPushButton("▼")
        bcima.clicked.connect(lambda: self._osd(lambda: self.camera.osd_seta(0, +1)))
        bbaixo.clicked.connect(lambda: self._osd(lambda: self.camera.osd_seta(0, -1)))
        besq.clicked.connect(lambda: self._osd(lambda: self.camera.osd_seta(-1, 0)))
        bdir.clicked.connect(lambda: self._osd(lambda: self.camera.osd_seta(+1, 0)))
        bok.clicked.connect(lambda: self._osd(self.camera.osd_ok))
        grade.addWidget(bcima, 0, 1)
        grade.addWidget(besq, 1, 0)
        grade.addWidget(bok, 1, 1)
        grade.addWidget(bdir, 1, 2)
        grade.addWidget(bbaixo, 2, 1)
        v.addLayout(grade)

        bvoltar = QtWidgets.QPushButton("↩  Voltar")
        bvoltar.clicked.connect(lambda: self._osd(self.camera.osd_voltar))
        v.addWidget(bvoltar)
        return g

    def _osd(self, funcao):
        # roda em segundo plano (o 'toque' das setas tem uma pausa curta)
        threading.Thread(target=funcao, daemon=True).start()

    def _grupo_exposicao(self):
        g = self._titulo("Exposição / White Balance")
        v = QtWidgets.QVBoxLayout(g)
        v.addWidget(QtWidgets.QLabel("White Balance"))
        self.cmb_wb = ComboSemRoda()
        self.cmb_wb.setFocusPolicy(QtCore.Qt.StrongFocus)
        self.cmb_wb.addItems(["Manter da câmera (não mexer)", "Automático",
                              "Interno (luz quente)", "Externo (luz fria)",
                              "Manual", "VAR (temperatura fixa)"])
        self._wb_modos = ["none", "auto", "indoor", "outdoor", "manual", "var"]
        atual = self.estado["cores"].get("white_balance", "none")
        self.cmb_wb.setCurrentIndex(self._wb_modos.index(atual) if atual in self._wb_modos else 0)
        self.cmb_wb.currentIndexChanged.connect(self._set_wb)
        v.addWidget(self.cmb_wb)
        b = QtWidgets.QPushButton("Calibrar branco agora (One Push)")
        b.clicked.connect(lambda: self.camera.wb_onepush())
        v.addWidget(b)

        # temperatura de cor (modo VAR) — 2500K a 8000K
        cab_ct = QtWidgets.QHBoxLayout()
        cab_ct.addWidget(QtWidgets.QLabel("Temperatura de cor (VAR):"))
        self.lbl_ct = QtWidgets.QLabel("4500K")
        cab_ct.addWidget(self.lbl_ct)
        cab_ct.addStretch(1)
        v.addLayout(cab_ct)
        self.sld_ct = self._slider(2500, 8000, 4500, self._set_color_temp, passo=100)
        v.addWidget(self.sld_ct)

        self.chk_bw = QtWidgets.QCheckBox("Preto e branco (B&W)")
        self.chk_bw.stateChanged.connect(lambda s: self._osd(lambda: self.camera.set_bw(bool(s))))
        v.addWidget(self.chk_bw)

        v.addWidget(QtWidgets.QLabel("Exposição (modo)"))
        self.cmb_exp = ComboSemRoda()
        self.cmb_exp.setFocusPolicy(QtCore.Qt.StrongFocus)
        self.cmb_exp.addItems(["Automática", "Manual", "Prioridade obturador",
                               "Prioridade íris", "Brilho"])
        self.cmb_exp.currentIndexChanged.connect(self._set_exp)
        v.addWidget(self.cmb_exp)
        v.addWidget(QtWidgets.QLabel("(para Íris/Shutter funcionarem, use Manual)"))

        # Íris (abertura de lente) — mostra o valor grande e atualiza sozinho
        cab_iris = QtWidgets.QHBoxLayout()
        cab_iris.addWidget(QtWidgets.QLabel("Íris:"))
        self.lbl_iris = QtWidgets.QLabel("—")
        self.lbl_iris.setStyleSheet("font-weight:bold;font-size:15px;color:#37d67a;")
        cab_iris.addWidget(self.lbl_iris)
        cab_iris.addStretch(1)
        v.addLayout(cab_iris)
        h1 = QtWidgets.QHBoxLayout()
        b_ia = QtWidgets.QPushButton("Abrir (+ clara)")
        b_if = QtWidgets.QPushButton("Fechar (+ escura)")
        b_ia.clicked.connect(lambda: self._ajustar_iris(+1))
        b_if.clicked.connect(lambda: self._ajustar_iris(-1))
        h1.addWidget(b_ia)
        h1.addWidget(b_if)
        v.addLayout(h1)

        # Shutter (velocidade do obturador)
        cab_sh = QtWidgets.QHBoxLayout()
        cab_sh.addWidget(QtWidgets.QLabel("Shutter:"))
        self.lbl_shutter = QtWidgets.QLabel("—")
        self.lbl_shutter.setStyleSheet("font-weight:bold;font-size:15px;color:#37d67a;")
        cab_sh.addWidget(self.lbl_shutter)
        cab_sh.addStretch(1)
        v.addLayout(cab_sh)
        h2 = QtWidgets.QHBoxLayout()
        b_su = QtWidgets.QPushButton("Mais rápido")
        b_sd = QtWidgets.QPushButton("Mais lento")
        b_su.clicked.connect(lambda: self._ajustar_shutter(+1))
        b_sd.clicked.connect(lambda: self._ajustar_shutter(-1))
        h2.addWidget(b_su)
        h2.addWidget(b_sd)
        v.addLayout(h2)

        # ganho e backlight (VISCA padrão)
        cab_g = QtWidgets.QHBoxLayout()
        cab_g.addWidget(QtWidgets.QLabel("Ganho:"))
        bgm = QtWidgets.QPushButton("+")
        bgd = QtWidgets.QPushButton("−")
        bgm.clicked.connect(lambda: self.camera.ganho_mais())
        bgd.clicked.connect(lambda: self.camera.ganho_menos())
        cab_g.addWidget(bgm)
        cab_g.addWidget(bgd)
        cab_g.addStretch(1)
        v.addLayout(cab_g)
        self.chk_blc = QtWidgets.QCheckBox("Backlight (compensa contraluz)")
        self.chk_blc.stateChanged.connect(lambda s: self.camera.set_backlight(bool(s)))
        v.addWidget(self.chk_blc)

        b = QtWidgets.QPushButton("↻ Puxar configurações da câmera")
        b.setToolTip("Lê da câmera os valores atuais (cores, exposição, white balance) "
                     "e ajusta os controles para bater com o que já está configurado.")
        b.clicked.connect(self._puxar_da_camera)
        v.addWidget(b)
        return g

    def _aplicar_tema(self):
        self.setStyleSheet("""
            QMainWindow, QWidget { background:#1e1f22; color:#e6e6e6; }
            QGroupBox { border:1px solid #3a3b40; border-radius:6px; margin-top:10px; }
            QPushButton { background:#2d2f34; border:1px solid #44464d; border-radius:4px;
                          padding:5px; }
            QPushButton:hover { background:#3a3d44; }
            QLineEdit, QComboBox { background:#26272b; border:1px solid #44464d;
                                   border-radius:4px; padding:3px; }
            QLabel { color:#c9ccd1; }
            QScrollArea { border:none; }
        """)

    # ---- callbacks de interface ------------------------------------------
    def _set_rastr(self, attr, val):
        """Ajusta um parâmetro de rastreamento e agenda salvar."""
        setattr(self, attr, val)
        self._timer_salvar.start(600)

    def _salvar_rastreamento(self):
        self.estado["rastreamento"] = {
            "escala_mult": self.escala_mult,
            "zoom_auto": self.zoom_auto,
            "tela_x": self.tela_x,
            "tela_y": self.tela_y,
            "margem": self.margem,
            "margem_vert": self.margem_vert,
            "firmeza_vert": self.firmeza_vert,
            "suav": self.suav,
            "antecipar": self.antecipar,
            "antecipar_ganho": self.antecipar_ganho,
            "seguir_vel": self.seguir_vel,
            "vman_pan": self.vman_pan,
            "vman_tilt": self.vman_tilt,
            "vman_zoom": self.vman_zoom,
        }
        salvar_estado(self.estado)

    def _cor(self, chave, valor, funcao):
        # NÃO envia na hora (HTTP trava a tela). Guarda e manda em segundo plano,
        # com um pequeno atraso — assim arrastar o slider não engasga o app.
        self.estado["cores"][chave] = valor
        self._cor_pendente[chave] = (valor, funcao)
        self._timer_cor.start(180)

    def _flush_cores(self):
        pend = dict(self._cor_pendente)
        self._cor_pendente.clear()
        salvar_estado(self.estado)

        def worker():
            for _chave, (val, func) in pend.items():
                try:
                    func(val)
                except Exception as e:
                    print("Aviso cor:", e)
        threading.Thread(target=worker, daemon=True).start()

    def _puxar_da_camera(self):
        """Lê da câmera as configurações atuais e ajusta os controles (não empurra nada)."""
        # NÃO inicia outra leitura se já houver uma rodando (evita travar/fechar o app)
        if getattr(self, "_leitor_cfg", None) is not None and self._leitor_cfg.isRunning():
            return
        self.statusBar().showMessage("Lendo configurações da câmera...", 2000)
        self._leitor_cfg = LeitorConfig(self)   # parent=self: o Qt gerencia a thread
        self._leitor_cfg.camera = self.camera
        self._leitor_cfg.pronto.connect(self._aplicar_config_lida)
        self._leitor_cfg.start()

    def _aplicar_config_lida(self, d):
        if not d:
            self.statusBar().showMessage(
                "Não consegui ler as configurações da câmera. Verifique IP/porta e se a "
                "câmera aceita VISCA-over-IP. (Deixei tudo como está.)", 9000)
            return
        modo = d.pop("_modo", "?")
        for chave, sld in [("saturacao", self.sld_sat), ("contraste", self.sld_con),
                           ("brilho", self.sld_bri), ("matiz", self.sld_mat),
                           ("nitidez", self.sld_nit)]:
            if d.get(chave) is not None:
                val = int(d[chave])
                if val > sld.maximum():        # câmera usa escala maior? amplia o slider
                    sld.setMaximum(val)
                sld.blockSignals(True)
                sld.setValue(val)
                sld.blockSignals(False)
                self.estado["cores"][chave] = val
        if d.get("wb") in self._wb_modos:
            self.cmb_wb.blockSignals(True)
            self.cmb_wb.setCurrentIndex(self._wb_modos.index(d["wb"]))
            self.cmb_wb.blockSignals(False)
            self.estado["cores"]["white_balance"] = d["wb"]
        exp_modos = ["auto", "manual", "shutter", "iris", "bright"]
        if d.get("exposicao") in exp_modos:
            self.cmb_exp.blockSignals(True)
            self.cmb_exp.setCurrentIndex(exp_modos.index(d["exposicao"]))
            self.cmb_exp.blockSignals(False)
        if "foco_auto" in d:
            self.chk_foco_auto.blockSignals(True)
            self.chk_foco_auto.setChecked(bool(d["foco_auto"]))
            self.chk_foco_auto.blockSignals(False)
        for chave, chk in [("flip", self.chk_flip), ("mirror", self.chk_mirror),
                           ("ldc", self.chk_ldc)]:
            if d.get(chave) is not None:
                chk.blockSignals(True)
                chk.setChecked(int(d[chave]) != 0)
                chk.blockSignals(False)
        if "shutter_pos" in d:
            self.ultimo_shutter_pos = d["shutter_pos"]
            self.lbl_shutter.setText(_rotulo_valor(SHUTTER_STR, d["shutter_pos"]))
        if "iris_pos" in d:
            self.ultimo_iris_pos = d["iris_pos"]
            self.lbl_iris.setText(_rotulo_valor(IRIS_STR, d["iris_pos"]))
        salvar_estado(self.estado)
        lidos = ", ".join(sorted(k for k in d.keys() if not k.startswith("_")))
        self.statusBar().showMessage(
            "Configurações puxadas da câmera (via %s): %s" % (modo, lidos), 6000)

    def _set_color_temp(self, kelvin):
        kelvin = int(round(kelvin / 100.0) * 100)
        self.lbl_ct.setText("%dK" % kelvin)
        self._osd(lambda: self.camera.set_color_temp(kelvin))

    def _set_wb(self, idx):
        m = self._wb_modos[idx] if 0 <= idx < len(self._wb_modos) else "none"
        self.estado["cores"]["white_balance"] = m
        salvar_estado(self.estado)
        self.camera.set_wb_modo(m)

    def _set_exp(self, idx):
        modos = ["auto", "manual", "shutter", "iris", "bright"]
        self.camera.set_exposicao_modo(modos[idx] if 0 <= idx < len(modos) else "auto")

    def _renomear_preset(self, i):
        self.estado["presets"][str(i)] = self.preset_edits[i].text()
        salvar_estado(self.estado)

    def _salvar_preset(self, i):
        # a câmera guarda posição + ZOOM + foco (distância) nativamente no preset.
        # aqui guardamos TODO o resto da imagem: cores, contraste, white balance,
        # exposição, íris e shutter (cada parte do palco tem luz diferente).
        self.camera.salvar_preset(i)
        exp_modos = ["auto", "manual", "shutter", "iris", "bright"]
        idx = self.cmb_exp.currentIndex()
        self.estado["presets_cfg"][str(i)] = {
            "cores": dict(self.estado["cores"]),   # sat, contraste, brilho, matiz, nitidez, wb
            "exposicao": exp_modos[idx] if 0 <= idx < len(exp_modos) else "auto",
            "iris_pos": self.ultimo_iris_pos,
            "shutter_pos": self.ultimo_shutter_pos,
            "foco_auto": self.chk_foco_auto.isChecked(),
        }
        salvar_estado(self.estado)
        extra = ""
        if self.ultimo_iris_pos is None and self.ultimo_shutter_pos is None:
            extra = " (íris/shutter serão guardados quando o 'Puxar' funcionar)"
        self.statusBar().showMessage(
            "Preset %d (%s) salvo com posição, zoom, foco, cores e exposição.%s" %
            (i, self.preset_edits[i].text(), extra), 5000)

    def _chamar_preset(self, i):
        # move a câmera (posição/zoom/foco que ela guardou)
        self.camera.chamar_preset(i)
        self.alvo_id = None
        self.alvo_box = None
        self._resetar_suavizacao()
        # segura os comandos até a câmera chegar (evita interromper o movimento)
        self._recall_ate = time.time() + 6.5
        # espera a câmera CHEGAR (posição do zoom parar de mudar) e só então troca as cores
        cfg = self.estado["presets_cfg"].get(str(i))
        self._espera_preset = EsperaChegada(self.camera, self.preset_delay_cores, self)
        self._espera_preset.chegou.connect(lambda c=cfg: self._preset_chegou(c))
        self._espera_preset.start()
        self.statusBar().showMessage("Indo para o preset %d (%s)..." %
                                     (i, self.preset_edits[i].text()), 3000)

    def _preset_chegou(self, cfg):
        """Chamado quando a câmera terminou de ir para o preset."""
        if cfg:
            self._aplicar_esquema(cfg)
        self._recall_ate = 0.0    # libera o controle/rastreamento (já chegou)

    def _aplicar_esquema(self, cfg):
        """Aplica tudo relacionado à imagem salvo num preset (cores, contraste,
        white balance, exposição, íris, shutter, foco)."""
        cores = cfg.get("cores", {})
        pares = [("saturacao", self.sld_sat, self.camera.set_saturacao),
                 ("contraste", self.sld_con, self.camera.set_contraste),
                 ("brilho", self.sld_bri, self.camera.set_brilho),
                 ("matiz", self.sld_mat, self.camera.set_matiz),
                 ("nitidez", self.sld_nit, self.camera.set_nitidez)]
        for chave, sld, funcao in pares:
            if chave in cores:
                val = int(cores[chave])
                sld.blockSignals(True)
                sld.setValue(val)
                sld.blockSignals(False)
                self.estado["cores"][chave] = val
                try:
                    funcao(val)
                except Exception:
                    pass
        wb = cores.get("white_balance")
        if wb in self._wb_modos:
            self.cmb_wb.blockSignals(True)
            self.cmb_wb.setCurrentIndex(self._wb_modos.index(wb))
            self.cmb_wb.blockSignals(False)
            self.estado["cores"]["white_balance"] = wb
            self.camera.set_wb_modo(wb)
        # exposição (modo)
        exp_modos = ["auto", "manual", "shutter", "iris", "bright"]
        if cfg.get("exposicao") in exp_modos:
            self.cmb_exp.blockSignals(True)
            self.cmb_exp.setCurrentIndex(exp_modos.index(cfg["exposicao"]))
            self.cmb_exp.blockSignals(False)
            self.camera.set_exposicao_modo(cfg["exposicao"])
        # íris e shutter (valor absoluto), se foram guardados
        if cfg.get("iris_pos") is not None:
            try:
                self.camera.set_iris_pos(int(cfg["iris_pos"]))
                self.ultimo_iris_pos = int(cfg["iris_pos"])
                self.lbl_iris.setText(_rotulo_valor(IRIS_STR, int(cfg["iris_pos"])))
            except Exception:
                pass
        if cfg.get("shutter_pos") is not None:
            try:
                self.camera.set_shutter_pos(int(cfg["shutter_pos"]))
                self.ultimo_shutter_pos = int(cfg["shutter_pos"])
                self.lbl_shutter.setText(_rotulo_valor(SHUTTER_STR, int(cfg["shutter_pos"])))
            except Exception:
                pass
        if "foco_auto" in cfg:
            self.chk_foco_auto.setChecked(bool(cfg["foco_auto"]))
        salvar_estado(self.estado)

    def _toggle_pausa(self):
        self.pausado = not self.pausado

    def _fixar_alvo(self, caixa):
        """Trava numa pessoa a partir da caixa dela (id, x1,y1,x2,y2)."""
        self.alvo_id = caixa[0]
        self.alvo_box = tuple(caixa[1:])
        self.alvo_vel = (0.0, 0.0)
        self.ultimo_visto = time.time()
        self._resetar_suavizacao()
        am = self._aims.get(caixa[0])
        self.alvo_aim = (float(am[0]), float(am[1])) if am else None

    def _cancelar_alvo(self):
        self.alvo_id = None
        self.alvo_box = None
        self.alvo_vel = (0.0, 0.0)
        self._resetar_suavizacao()

    def _proxima_pessoa(self):
        if not self.caixas:
            return
        # escolhe a próxima pela ordem horizontal (esquerda -> direita)
        ordenadas = sorted(self.caixas, key=lambda c: (c[1] + c[3]) / 2.0)
        if self.alvo_box is None:
            self._fixar_alvo(ordenadas[0])
            return
        acx = (self.alvo_box[0] + self.alvo_box[2]) / 2.0
        prox = None
        for c in ordenadas:
            if (c[1] + c[3]) / 2.0 > acx + 5:
                prox = c
                break
        self._fixar_alvo(prox if prox else ordenadas[0])

    def _seguir_central(self):
        """Escolhe a pessoa mais próxima do centro (útil no controle)."""
        if not self.caixas:
            return
        cx = self.W / 2.0
        self._fixar_alvo(min(self.caixas, key=lambda c: abs((c[1] + c[3]) / 2.0 - cx)))

    def _clique_video(self, x, y):
        alvo = None
        area = None
        for c in self.caixas:
            _id, x1, y1, x2, y2 = c
            if x1 <= x <= x2 and y1 <= y <= y2:
                a = (x2 - x1) * (y2 - y1)
                if area is None or a < area:
                    area, alvo = a, c
        if alvo is not None:
            self._fixar_alvo(alvo)

    def _novas_caixas(self, dados):
        # dados = lista de (id, x1, y1, x2, y2, aim_x, aim_y, aim_ok)
        self.caixas = [d[:5] for d in dados]
        self._aims = {d[0]: (d[5], d[6], d[7]) for d in dados}

    # ---- teclado ----------------------------------------------------------
    def keyPressEvent(self, ev):
        if ev.isAutoRepeat():
            return
        k = ev.key()
        if k == QtCore.Qt.Key_Escape:
            self.close()
        elif k == QtCore.Qt.Key_Space:
            self._toggle_pausa()
        elif k == QtCore.Qt.Key_N:
            self._proxima_pessoa()
        elif k == QtCore.Qt.Key_C:
            self._cancelar_alvo()
        # presets agora são Ctrl+1..9 (atalho global, ver _montar_atalhos)
        else:
            self.teclas.add(k)

    def keyReleaseEvent(self, ev):
        if ev.isAutoRepeat():
            return
        self.teclas.discard(ev.key())

    def eventFilter(self, obj, ev):
        # Reaproveita keyPressEvent/keyReleaseEvent (janela principal) para
        # teclas que chegam na 2ª TELA (self.win_ctrl é uma janela separada,
        # então o teclado nem chegava a ela). Não intercepta se o widget com
        # foco for um campo de TEXTO (deixa digitar normalmente, ex.: nome
        # de preset) — igual ao que já acontece na janela principal.
        t = ev.type()
        if t in (QtCore.QEvent.KeyPress, QtCore.QEvent.KeyRelease):
            if (isinstance(obj, QtWidgets.QWidget) and obj.window() is self.win_ctrl
                    and not isinstance(obj, (QtWidgets.QLineEdit, QtWidgets.QTextEdit))):
                if t == QtCore.QEvent.KeyPress:
                    self.keyPressEvent(ev)
                else:
                    self.keyReleaseEvent(ev)
        return False

    def _mov_teclado(self):
        pan = tilt = zoom = 0
        if QtCore.Qt.Key_D in self.teclas:
            pan += self.vman_pan
        if QtCore.Qt.Key_A in self.teclas:
            pan -= self.vman_pan
        if QtCore.Qt.Key_W in self.teclas:
            tilt += self.vman_tilt
        if QtCore.Qt.Key_S in self.teclas:
            tilt -= self.vman_tilt
        if QtCore.Qt.Key_Q in self.teclas:
            zoom += self.vman_zoom
        if QtCore.Qt.Key_E in self.teclas:
            zoom -= self.vman_zoom
        return pan, tilt, zoom

    # ---- laço de controle -------------------------------------------------
    def _controle(self):
        pan, tilt, zoom = self._mov_teclado()

        # gamepad
        gp = self.gamepad.ler()
        for ev in gp["eventos"]:
            if ev == "preset1":
                self._chamar_preset(1)
            elif ev == "preset2":
                self._chamar_preset(2)
            elif ev == "preset3":
                self._chamar_preset(3)
            elif ev == "preset4":
                self._chamar_preset(4)
            elif ev == "salva1":
                self._salvar_preset(1)
            elif ev == "salva2":
                self._salvar_preset(2)
            elif ev == "salva3":
                self._salvar_preset(3)
            elif ev == "salva4":
                self._salvar_preset(4)
            elif ev == "pausa":       # R1 = para de seguir
                self.pausado = True
            elif ev == "resume":      # R2 = volta a seguir
                self.pausado = False
            elif ev == "cancela":     # L1 = cancela seguir
                self._cancelar_alvo()
            elif ev == "central":     # L2 = segue a pessoa central
                self._seguir_central()

        # foco pelo analógico direito (X): direita/esquerda
        gfoco = gp["foco"]
        if abs(gfoco) > 0.3:
            self._foco_gamepad(gfoco)
        else:
            self._foco_gamepad(0.0)

        pan += int(round(gp["pan"] * self.vman_pan))
        tilt += int(round(gp["tilt"] * self.vman_tilt))
        zoom += int(round(gp["zoom"] * self.vman_zoom))

        manual = bool(pan or tilt or zoom)
        if manual:
            self.detector.ativo = False
            self.camera.mover(pan, tilt, zoom)
            self.modo_txt = "MANUAL"
            self._recall_ate = 0.0     # o usuário assumiu: cancela a espera do preset
            return
        self.detector.ativo = True

        # enquanto a câmera está indo para um preset, NÃO manda comando nenhum
        # (senão o "parar" interrompe o movimento e o zoom volta errado).
        if time.time() < self._recall_ate:
            self.modo_txt = "INDO AO PRESET..."
            return

        if self.pausado:
            self.camera.parar()
            self.modo_txt = "PAUSADO"
            return

        box, achou = self._atualizar_alvo()
        if box is not None and achou:
            p, t, z = self._calcular_movimento(box)
            self.camera.mover(p, t, z)
            self.modo_txt = "SEGUINDO"
        elif box is not None:
            # perdeu momentaneamente (ex.: alguém passou na frente): SEGURA PARADO,
            # não sai perseguindo fantasma. Solta de vez só após o tempo limite.
            self.camera.parar()
            self.modo_txt = "PROCURANDO..."
            if time.time() - self.ultimo_visto > self.timeout_perda:
                self.alvo_box = None
                self.alvo_id = None
        else:
            self.camera.parar()
            self.modo_txt = "SEM ALVO"

    def _resetar_suavizacao(self):
        self.sx = None
        self.sy = None
        self.mov_x = False
        self.mov_y = False
        self.vp = 0.0
        self.vt = 0.0
        self.alvo_aim = None
        self.aim_confiavel = False
        self.sh = None
        self._zoom_ativo = False

    def _atualizar_alvo(self):
        """Segue a pessoa pela CAIXA (posição+tamanho), ignorando o número do
        YOLO (que fica trocando). Retorna (caixa_ou_None, achou_neste_quadro)."""
        if self.alvo_box is None:
            return None, False

        lx1, ly1, lx2, ly2 = self.alvo_box
        lcx, lcy = (lx1 + lx2) / 2.0, (ly1 + ly2) / 2.0
        lh = max(1.0, ly2 - ly1)
        # posição prevista (usa a velocidade estimada)
        pcx, pcy = lcx + self.alvo_vel[0], lcy + self.alvo_vel[1]

        gate = max(self.raio_reid, 0.12) * self.W   # distância máxima aceitável
        melhor, melhor_cost, melhor_d = None, None, None
        for c in self.caixas:
            _id, x1, y1, x2, y2 = c
            cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
            d = ((cx - pcx) ** 2 + (cy - pcy) ** 2) ** 0.5
            if d > gate:
                continue
            razao = (y2 - y1) / lh
            # barra quem passa na frente (fica maior) ou muito diferente
            if not (0.7 <= razao <= 1.4):
                continue
            custo = d + abs(1.0 - razao) * self.W * 0.15
            if melhor_cost is None or custo < melhor_cost:
                melhor_cost, melhor, melhor_d = custo, c, d

        if melhor is not None:
            _id, x1, y1, x2, y2 = melhor
            ncx, ncy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
            # velocidade suavizada (previsão)
            self.alvo_vel = (0.6 * self.alvo_vel[0] + 0.4 * (ncx - lcx),
                             0.6 * self.alvo_vel[1] + 0.4 * (ncy - lcy))
            # caixa suavizada (converge rápido p/ não atrasar, mas filtra tremor)
            a = 0.7
            self.alvo_box = (lx1 + a * (x1 - lx1), ly1 + a * (y1 - ly1),
                             lx2 + a * (x2 - lx2), ly2 + a * (y2 - ly2))
            self.alvo_id = _id
            # ponto de mira da pose (ombros/cabeça). Só atualiza em X se a pose
            # estiver CONFIÁVEL (não segue as mãos quando levanta os braços).
            am = self._aims.get(_id)
            if am is not None:
                ax_, ay_, ok = am
                self.aim_confiavel = ok
                if self.alvo_aim is None:
                    self.alvo_aim = (float(ax_), float(ay_))
                elif ok:
                    self.alvo_aim = (0.35 * self.alvo_aim[0] + 0.65 * ax_,
                                     0.35 * self.alvo_aim[1] + 0.65 * ay_)
                else:
                    # pose momentaneamente não confiável (cabeça muito baixa/de
                    # lado, movimento rápido). Antes o Y ficava TRAVADO até a
                    # pose voltar — se o ministro abaixasse/levantasse rápido, a
                    # câmera perdia o rosto e cortava a cabeça. Agora, em Y (só
                    # Y — X continua travado, não segue mãos), acompanha de leve
                    # o TOPO da caixa (existe mesmo sem pose, é bem mais estável
                    # que os keypoints em movimento rápido) até a pose voltar.
                    alt_c = max(1.0, y2 - y1)
                    ay_reserva = y1 + alt_c * 0.12
                    self.alvo_aim = (self.alvo_aim[0],
                                     0.8 * self.alvo_aim[1] + 0.2 * ay_reserva)
            self.ultimo_visto = time.time()
            return self.alvo_box, True

        # nada casou neste quadro: mantém a caixa (coasting), velocidade decai
        self.alvo_vel = (self.alvo_vel[0] * 0.7, self.alvo_vel[1] * 0.7)
        self.alvo_id = None
        return self.alvo_box, False

    def _calcular_movimento(self, box):
        x1, y1, x2, y2 = box
        # ponto de mira: prioriza a POSE (ombros p/ X, cabeça p/ Y) — braço aberto
        # não desloca a mira. Sem pose, cai no centro da caixa.
        if self.alvo_aim is not None:
            tx, ty = float(self.alvo_aim[0]), float(self.alvo_aim[1])
        else:
            tx = (x1 + x2) / 2.0
            ty = y1 + (y2 - y1) * self.mira_v

        # 1) SUAVIZAÇÃO: média móvel do alvo (braço abrindo / tremor da detecção
        #    não viram movimento de câmera). A VERTICAL é mais suave (a cabeça
        #    balança naturalmente) para não ficar corrigindo a altura toda hora.
        if self.sx is None:
            self.sx, self.sy = tx, ty
        else:
            # piso de responsividade: mesmo com "Suavidade" alta, não arrasta demais.
            fx = min(1.0, self.suav * 2.0 + 0.25)
            self.sx += fx * (tx - self.sx)
            # FIRMEZA VERTICAL: um tremor pequeno de cima/baixo (respirar, balançar
            # a cabeça ao falar) nem chega a mexer no alvo interno — só desloca de
            # verdade quando o movimento vertical é maior que este limiar. Isso evita
            # ficar "corrigindo por pouca coisa" antes mesmo de qualquer margem/zona.
            limiar_y = self.firmeza_vert * self.H
            dy = ty - self.sy
            if abs(dy) > limiar_y:
                self.sy += (fx * 0.7) * dy

        # 1b) ANTECIPAÇÃO: quando o alvo se move para um lado, mira um pouco À FRENTE
        #     dele (na direção do movimento), fazendo a câmera "chumbar" e continuar
        #     indo pro lado que ele foi, em vez de correr sempre atrás.
        alvo_x = self.sx
        if self.antecipar:
            alvo_x = self.sx + self.antecipar_ganho * self.alvo_vel[0]

        ex = (alvo_x - self.tela_x * self.W) / (self.W / 2.0)
        ey = (self.sy - self.tela_y * self.H) / (self.H / 2.0)

        # 2) MARGEM DE SEGURANÇA com histerese: só começa a mexer quando a pessoa
        #    sai da margem; só para quando ela volta bem para perto do centro.
        # "Descanso" após parar: por um instante não recomeça a mexer (dá tempo do
        # vídeo, que tem atraso, mostrar a posição nova) — evita ficar oscilando.
        # Só ignora o descanso se a pessoa saiu MUITO do centro (andou de verdade).
        em_settle = time.time() < self._settle_ate
        margem_y = self.margem_vert     # margem vertical AJUSTÁVEL (não cortar a cabeça)
        gx = self.margem * (2.0 if em_settle else 1.0)
        gy = margem_y * (2.0 if em_settle else 1.0)
        if self.mov_x:
            if abs(ex) < self.parar_em:
                self.mov_x = False
        elif abs(ex) > gx:
            self.mov_x = True
        if self.mov_y:
            if abs(ey) < self.parar_em:
                self.mov_y = False
        elif abs(ey) > gy:
            self.mov_y = True

        movendo = self.mov_x or self.mov_y
        if self._mov_ativo and not movendo:          # acabou de parar agora
            self._settle_ate = time.time() + 0.3
        self._mov_ativo = movendo

        # Velocidade desejada = PROPORCIONAL ao erro (quanto mais longe do centro,
        # mais rápido). Isto já desacelera sozinho ao centralizar (não passa do ponto).
        alvo_vp = (self.ganho_pan * self.escala_mult * ex) if self.mov_x else 0.0
        alvo_vt = (-self.ganho_tilt * self.escala_mult * ey) if self.mov_y else 0.0

        # 3) ACELERAÇÃO SUAVE: limita o quanto a velocidade muda por passo (sem tranco).
        self.vp += max(-self.acel, min(self.acel, alvo_vp - self.vp))
        self.vt += max(-self.acel, min(self.acel, alvo_vt - self.vt))
        if not self.mov_x and abs(self.vp) < 0.6:
            self.vp = 0.0
        if not self.mov_y and abs(self.vt) < 0.6:
            self.vt = 0.0

        # 3b) TURBO ANTI-FUGA (só o TETO): perto do centro o teto é o normal (câmera
        #     firme, sem oscilar); quando o pastor se aproxima da BORDA, o teto SOBE
        #     pra deixar a câmera correr atrás sem estourar. Como a velocidade é
        #     proporcional ao erro, ela freia sozinha ao centralizar — não passa do
        #     ponto. |ex|=1 = alvo na beirada da meia-tela. Nunca passa do hardware.
        tbx = 1.0 + (self.seguir_vel - 1.0) * min(1.0, abs(ex))
        tby = 1.0 + (self.seguir_vel - 1.0) * min(1.0, abs(ey))
        vmp = min(24.0, self.vmax_pan * tbx)
        vmt = min(20.0, self.vmax_tilt * tby)
        pan = int(max(-vmp, min(vmp, round(self.vp))))
        tilt = int(max(-vmt, min(vmt, round(self.vt))))

        zoom = 0
        if self.zoom_auto:
            alt = float(y2 - y1)
            # altura BEM suavizada: postura/braço/ruído não disparam zoom
            if self.sh is None:
                self.sh = alt
            else:
                self.sh += 0.12 * (alt - self.sh)
            frac = self.sh / float(self.H)
            # histerese larga p/ começar, estreita p/ parar; zoom lento (1).
            # Só recomeça se estiver bem fora do alvo -> não fica dando zoom do nada.
            if self._zoom_ativo:
                if frac < self.zoom_alvo - 0.05:
                    zoom = 1
                elif frac > self.zoom_alvo + 0.05:
                    zoom = -1
                else:
                    self._zoom_ativo = False
            else:
                if frac < self.zoom_alvo - 0.16:
                    zoom = 1
                    self._zoom_ativo = True
                elif frac > self.zoom_alvo + 0.16:
                    zoom = -1
                    self._zoom_ativo = True
        return pan, tilt, zoom

    # ---- render -----------------------------------------------------------
    def _render(self):
        raw = self.video.ler()
        if raw is None:
            return
        h0, w0 = raw.shape[:2]
        esc = self.proc_w / float(w0)
        frame = cv2.resize(raw, (self.proc_w, int(h0 * esc)))
        self.H, self.W = frame.shape[:2]

        mx, my = int(self.tela_x * self.W), int(self.tela_y * self.H)

        # --- GUIAS DE ENQUADRAMENTO (estilo OBS) ---
        W, H = self.W, self.H
        cxW, cyH = W // 2, H // 2   # centro real da imagem
        amarelo = (0, 255, 255)
        ciano = (0, 200, 200)
        # 1) retângulo de área de segurança NAS BORDAS (5% de cada lado)
        mb_x, mb_y = int(W * 0.05), int(H * 0.05)
        cv2.rectangle(frame, (mb_x, mb_y), (W - mb_x, H - mb_y), (0, 170, 255), 1)
        # 2) marcas do CENTRO em cada borda (pra saber o meio dos 4 lados)
        t = 22
        cv2.line(frame, (cxW, 0), (cxW, t), amarelo, 2)              # topo
        cv2.line(frame, (cxW, H - t), (cxW, H), amarelo, 2)          # base
        cv2.line(frame, (0, cyH), (t, cyH), amarelo, 2)             # esquerda
        cv2.line(frame, (W - t, cyH), (W, cyH), amarelo, 2)          # direita
        # 3) cruz central leve (referência do meio da imagem)
        cv2.line(frame, (cxW, 0), (cxW, H), ciano, 1)
        cv2.line(frame, (0, cyH), (W, cyH), ciano, 1)
        # 4) ZONA DO PASTOR (onde a câmera tenta mantê-lo; enquanto ele está DENTRO
        #    dela a câmera fica parada). Largura = "Margem de segurança"; ALTURA =
        #    "Margem vertical". Assim, ao mexer nos sliders, este retângulo muda ao
        #    vivo e você vê exatamente quanto de folga está dando.
        cv2.drawMarker(frame, (mx, my), (0, 220, 0), cv2.MARKER_CROSS, 30, 2)
        ox = int(self.margem * W / 2.0)
        oy = int(self.margem_vert * H / 2.0)     # <- agora usa a margem VERTICAL
        cv2.rectangle(frame, (mx - ox, my - oy), (mx + ox, my + oy), (0, 220, 0), 2)
        cv2.putText(frame, "zona do pastor", (mx - ox, max(15, my - oy - 8)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 220, 0), 1, cv2.LINE_AA)

        # 5) RASTREIO DO ROSTO/POSE: mostra exatamente o ponto que a IA está
        #    usando pra mirar (ombros em X, cabeça em Y) — ajuda a ver na hora
        #    quando ele pega muito alto/muito baixo. Ciano = pose confiável
        #    (rosto/ombros detectados); laranja = "modo reserva" (pose sumiu
        #    por um instante — cabeça muito baixa/rápida — seguindo pelo topo
        #    da caixa até a pose voltar).
        if self.alvo_aim is not None:
            ax, ay = int(self.alvo_aim[0]), int(self.alvo_aim[1])
            cor_aim = (255, 220, 0) if self.aim_confiavel else (0, 165, 255)
            cv2.circle(frame, (ax, ay), 9, cor_aim, 2)
            cv2.drawMarker(frame, (ax, ay), cor_aim, cv2.MARKER_TILTED_CROSS, 14, 2)
            cv2.putText(frame, "rosto" if self.aim_confiavel else "rosto (reserva)",
                        (ax + 14, ay + 5), cv2.FONT_HERSHEY_SIMPLEX, 0.45, cor_aim, 1,
                        cv2.LINE_AA)

        # outras pessoas em cinza
        for c in self.caixas:
            _id, x1, y1, x2, y2 = c
            cv2.rectangle(frame, (x1, y1), (x2, y2), (200, 200, 200), 1)

        # o ALVO em verde, desenhado pela caixa rastreada (segue mesmo com o número trocando)
        if self.alvo_box is not None:
            bx1, by1, bx2, by2 = [int(v) for v in self.alvo_box]
            cv2.rectangle(frame, (bx1, by1), (bx2, by2), (0, 220, 0), 3)
            cv2.putText(frame, "ALVO", (bx1, max(15, by1 - 6)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 220, 0), 2, cv2.LINE_AA)

        # fps
        agora = time.time()
        dt = agora - self._fps_t
        self._fps_t = agora
        if dt > 0:
            self.fps = 0.9 * self.fps + 0.1 * (1.0 / dt)

        self.video_lbl.mostrar(frame)
        cor_status = {"SEGUINDO": "#37d67a", "MANUAL": "#f5a623",
                      "PAUSADO": "#f5a623", "PROCURANDO...": "#f5a623"}.get(self.modo_txt, "#8a8f98")
        self.lbl_status.setText(self.modo_txt)
        self.lbl_status.setStyleSheet("font-size:16px;font-weight:bold;color:%s;" % cor_status)

    # ---- encerrar ---------------------------------------------------------
    def closeEvent(self, ev):
        try:
            self._salvar_layout()   # lembra a disposição dos painéis
            self.win_ctrl.close()   # fecha a 2ª tela junto
            self.t_ctrl.stop()
            self.t_render.stop()
            if self._leitor_cfg is not None:
                self._leitor_cfg.wait(1000)
            self.detector.parar()
            self.camera.fechar()
            self.video.parar()
        except Exception:
            pass
        ev.accept()


def main():
    app = QtWidgets.QApplication(sys.argv)
    win = Janela()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
