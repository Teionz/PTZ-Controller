# -*- coding: utf-8 -*-
"""
Rastreador de pessoa para câmera PTZ (PTZOptics / NeoiD).

- Recebe o vídeo da câmera pela rede (RTSP).
- Usa IA (YOLO) para detectar as pessoas, de frente, de lado ou de costas.
- Você CLICA na pessoa que quer seguir; a câmera passa a segui-la sozinha.
- Comanda a câmera pela rede (VISCA sobre IP ou HTTP), sem depender do OBS.

Controles (com a janela do vídeo em foco):
    Clique      -> seleciona a pessoa que quer seguir
    N           -> troca para a próxima pessoa detectada
    C           -> cancela a seleção (câmera para)
    Espaço      -> pausa / retoma o rastreamento
    + / -       -> aumenta / diminui a velocidade de resposta
    Q ou ESC    -> sai (a câmera é parada com segurança)
"""

import configparser
import os
import shutil
import socket
import sys
import threading
import time

import cv2
import numpy as np
import requests


# ----------------------------------------------------------------------------
# Caminhos — funciona igual rodando pelo Python E instalado como .exe.
#   * Recursos (config.ini padrão, modelo de IA): acompanham o programa.
#   * Dados graváveis (estado.json e o config.ini editável): NÃO podem ficar
#     na pasta do programa quando instalado em "Arquivos de Programas" (é
#     somente-leitura), então vão para %APPDATA%\RastreadorPTZ.
# ----------------------------------------------------------------------------
APP_NOME = "RastreadorPTZ"


def _frozen():
    return getattr(sys, "frozen", False)


def pasta_recursos():
    """Pasta dos arquivos que ACOMPANHAM o programa. Pelo Python = pasta do
    código; como .exe (PyInstaller) = pasta do bundle."""
    if _frozen():
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def pasta_dados():
    """Pasta GRAVÁVEL dos dados do usuário. Instalado = %APPDATA%\\RastreadorPTZ;
    rodando pelo Python = a própria pasta do código (comportamento de antes)."""
    if _frozen():
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        d = os.path.join(base, APP_NOME)
    else:
        d = os.path.dirname(os.path.abspath(__file__))
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return d


def caminho_config():
    """config.ini editável (na pasta de dados). Na 1a vez copia o padrão que veio
    com o programa, para o usuário editar sem mexer em Arquivos de Programas."""
    destino = os.path.join(pasta_dados(), "config.ini")
    if not os.path.exists(destino):
        padrao = os.path.join(pasta_recursos(), "config.ini")
        try:
            if os.path.exists(padrao) and os.path.abspath(padrao) != os.path.abspath(destino):
                shutil.copyfile(padrao, destino)
        except Exception:
            pass
    return destino if os.path.exists(destino) else os.path.join(pasta_recursos(), "config.ini")


def caminho_modelo(nome):
    """Se o modelo (.pt) veio junto com o programa, usa o caminho absoluto dele.
    Senão, aponta para a pasta GRAVÁVEL de dados (nunca a pasta do código/nome
    puro) — instalado em "Arquivos de Programas" é somente-leitura, e se o app
    tentasse baixar/salvar o modelo ali o download falharia sem aviso claro
    (era exatamente o bug: modelo não veio junto -> tentava baixar em pasta
    sem permissão -> IA nunca subia). Assim, se faltar o modelo bundled, a IA
    ainda consegue baixar (com internet) e salvar num lugar que ela pode escrever."""
    junto = os.path.join(pasta_recursos(), os.path.basename(nome))
    if os.path.exists(junto):
        return junto
    return os.path.join(pasta_dados(), os.path.basename(nome))


# ----------------------------------------------------------------------------
# Leitura do estado REAL das teclas (sem depender do auto-repeat do Windows).
# É isto que dá a resposta "de videogame" ao controle manual: a cada quadro
# perguntamos "esta tecla está pressionada agora?".
# ----------------------------------------------------------------------------
_IS_WIN = sys.platform == "win32"
if _IS_WIN:
    import ctypes
    _user32 = ctypes.windll.user32

# códigos das teclas no Windows
_VK = {"W": 0x57, "A": 0x41, "S": 0x53, "D": 0x44, "Q": 0x51, "E": 0x45}


def _tecla_pressionada(nome):
    if not _IS_WIN:
        return False
    return (_user32.GetAsyncKeyState(_VK[nome]) & 0x8000) != 0


def _janela_em_foco(titulo):
    """Só aceita o teclado manual quando a janela do app está na frente,
    para não mover a câmera enquanto você digita em outro programa."""
    if not _IS_WIN:
        return True
    try:
        hwnd = _user32.FindWindowW(None, titulo)
        if not hwnd:
            return True  # não achou a janela: não bloqueia
        return _user32.GetForegroundWindow() == hwnd
    except Exception:
        return True


# ----------------------------------------------------------------------------
# Configuração
# ----------------------------------------------------------------------------
def carregar_config():
    cfg = configparser.ConfigParser()
    caminho = caminho_config()
    if not os.path.exists(caminho):
        print("ERRO: arquivo config.ini não encontrado ao lado do programa.")
        sys.exit(1)
    cfg.read(caminho, encoding="utf-8")
    return cfg


# ----------------------------------------------------------------------------
# Controle da câmera (movimento)
# ----------------------------------------------------------------------------
class ControladorCamera:
    """Envia comandos de movimento contínuo para a câmera e sabe parar com segurança."""

    def __init__(self, cfg):
        self.ip = cfg.get("camera", "ip")
        self.protocolo = cfg.get("controle", "protocolo", fallback="visca").strip().lower()
        self.visca_porta = cfg.getint("controle", "visca_porta", fallback=1259)
        self.http_porta = cfg.getint("controle", "http_porta", fallback=80)
        self.inverter_pan = cfg.getboolean("controle", "inverter_pan", fallback=False)
        self.inverter_tilt = cfg.getboolean("controle", "inverter_tilt", fallback=False)
        self.wb_var_codigo = cfg.getint("cores", "wb_var_codigo", fallback=0x20)
        # comandos do menu interno (OSD), configuráveis por serem específicos do modelo
        self.osd_cmd_menu = cfg.get("osd", "menu", fallback="81 01 06 06 10 FF")
        self.osd_cmd_ok = cfg.get("osd", "ok", fallback="81 01 06 06 05 FF")
        self.osd_cmd_voltar = cfg.get("osd", "voltar", fallback="81 01 06 06 04 FF")
        self.osd_vel_seta = cfg.getint("osd", "vel_seta", fallback=6)

        self._ultimo_cmd = None
        self._ultimo_envio = 0.0
        # Socket VISCA sempre disponível: os comandos de cor/exposição/preset são
        # VISCA padrão, mesmo quando o MOVIMENTO é feito por HTTP.
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.settimeout(0.3)

    # --- VISCA cru (usado por cor/exposição/preset) -----------------------
    def _visca(self, *bytes_):
        try:
            self._sock.sendto(bytes(bytes_), (self.ip, self.visca_porta))
        except Exception as e:
            print("Aviso: falha VISCA:", e)

    @staticmethod
    def _nib2(v):
        """Valor 0-255 -> dois bytes de nibble (formato 00 00 0p 0q do VISCA)."""
        v = max(0, min(255, int(v)))
        return [0x00, 0x00, (v >> 4) & 0x0F, v & 0x0F]

    # --- Cor / imagem (via HTTP, valores nativos da câmera 0-14) -----------
    def set_saturacao(self, v):
        self.set_imagem_http("saturation", v)

    def set_matiz(self, v):
        self.set_imagem_http("hue", v)

    def set_nitidez(self, v):
        self.set_imagem_http("sharpness", v)

    def set_brilho(self, v):
        self.set_imagem_http("bright", v)

    # --- Imagem via HTTP (get_image_conf / post_image_value) --------------
    # A câmera responde bright/saturation/contrast/sharpness/hue por HTTP
    # (confirmado no diagnóstico). Valores nativos (ex.: 0-14).
    def ler_imagem_http(self, timeout=1.5):
        import re
        try:
            import requests
            r = requests.get("http://%s:%d/cgi-bin/param.cgi?get_image_conf"
                             % (self.ip, self.http_porta), timeout=timeout)
            d = {}
            for chave, val in re.findall(r'(\w+)\s*=\s*"?(-?\d+)"?', r.text):
                try:
                    d[chave] = int(val)
                except Exception:
                    pass
            return d
        except Exception:
            return {}

    def set_imagem_http(self, chave, valor, timeout=1.0):
        try:
            import requests
            requests.get("http://%s:%d/cgi-bin/param.cgi?post_image_value&%s&%d"
                         % (self.ip, self.http_porta, chave, int(valor)), timeout=timeout)
        except Exception as e:
            print("Aviso set imagem http:", e)

    def set_contraste(self, valor):
        self.set_imagem_http("contrast", valor)

    # imagem: flip/mirror/LDC por HTTP (confirmados no get_image_conf da câmera)
    def set_flip(self, on):
        self.set_imagem_http("flip", 1 if on else 0)

    def set_mirror(self, on):
        self.set_imagem_http("mirror", 1 if on else 0)

    def set_ldc(self, on):
        self.set_imagem_http("ldc", 1 if on else 0)

    # exposição extra por VISCA (padrão Sony)
    def ganho_mais(self):
        self._visca(0x81, 0x01, 0x04, 0x0C, 0x02, 0xFF)

    def ganho_menos(self):
        self._visca(0x81, 0x01, 0x04, 0x0C, 0x03, 0xFF)

    def set_backlight(self, on):
        self._visca(0x81, 0x01, 0x04, 0x33, 0x02 if on else 0x03, 0xFF)

    def set_bw(self, on):
        # preto-e-branco: efeito de imagem VISCA (Picture Effect: 04=B&W, 00=off)
        self._visca(0x81, 0x01, 0x04, 0x63, 0x04 if on else 0x00, 0xFF)

    def set_color_temp(self, kelvin):
        # temperatura de cor no modo VAR. índice = (K - 2500)/100 (padrão comum).
        idx = max(0, min(55, int(round((kelvin - 2500) / 100.0))))
        self._visca(0x81, 0x01, 0x04, 0x20, (idx >> 4) & 0x0F, idx & 0x0F, 0xFF)

    def ler_pos_iris_shutter(self, timeout=0.35):
        """Lê a posição real de íris e shutter (uma leitura rápida, sob demanda).
        Retorna (iris, shutter) — cada um pode ser None se não responder."""
        import socket as _socket
        s = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
        s.settimeout(timeout)
        iris = shutter = None

        def q(*b):
            try:
                s.sendto(bytes(b), (self.ip, self.visca_porta))
                return self._payload(s.recvfrom(64)[0])
            except Exception:
                return None

        try:
            p = q(0x81, 0x09, 0x04, 0x4B, 0xFF)          # Íris
            if p and len(p) >= 2:
                iris = ((p[-2] & 0x0F) << 4) | (p[-1] & 0x0F)
            p = q(0x81, 0x09, 0x04, 0x4A, 0xFF)          # Shutter
            if p and len(p) >= 2:
                shutter = ((p[-2] & 0x0F) << 4) | (p[-1] & 0x0F)
        finally:
            s.close()
        return iris, shutter

    def _ler_pos4(self, sub, timeout=0.3):
        """Lê uma posição de 4 nibbles (zoom 0x47, foco 0x48)."""
        import socket as _socket
        s = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
        s.settimeout(timeout)
        try:
            s.sendto(bytes([0x81, 0x09, 0x04, sub, 0xFF]), (self.ip, self.visca_porta))
            p = self._payload(s.recvfrom(64)[0])
            if p and len(p) >= 4:
                return (((p[-4] & 0xF) << 12) | ((p[-3] & 0xF) << 8) |
                        ((p[-2] & 0xF) << 4) | (p[-1] & 0xF))
            return None
        except Exception:
            return None
        finally:
            s.close()

    def ler_pos_zoom(self, timeout=0.3):
        return self._ler_pos4(0x47, timeout)

    def ler_pos_foco(self, timeout=0.3):
        return self._ler_pos4(0x48, timeout)

    def set_pos_foco(self, pos):
        """Foco em posição ABSOLUTA (preciso). Precisa estar em foco manual."""
        pos = max(0, min(0xFFFF, int(pos)))
        self._visca(0x81, 0x01, 0x04, 0x48, (pos >> 12) & 0xF, (pos >> 8) & 0xF,
                    (pos >> 4) & 0xF, pos & 0xF, 0xFF)

    def set_iris_pos(self, pos):        # valor absoluto (para recall de preset)
        self._visca(0x81, 0x01, 0x04, 0x4B, *self._nib2(pos), 0xFF)

    def set_shutter_pos(self, pos):     # valor absoluto (para recall de preset)
        self._visca(0x81, 0x01, 0x04, 0x4A, *self._nib2(pos), 0xFF)

    # --- Balanço de branco (white balance) --------------------------------
    def set_wb_modo(self, modo):
        # "none" = não altera (mantém o que está no menu da câmera, ex.: VAR configurado)
        if modo == "none":
            return
        # 0=Auto 1=Interno 2=Externo 3=OnePush 5=Manual  var=código configurável (0x20)
        m = {"auto": 0, "indoor": 1, "outdoor": 2, "onepush": 3, "manual": 5,
             "var": self.wb_var_codigo}.get(modo, 0)
        self._visca(0x81, 0x01, 0x04, 0x35, m, 0xFF)

    def wb_onepush(self):
        self._visca(0x81, 0x01, 0x04, 0x10, 0x05, 0xFF)

    def set_ganho_vermelho(self, pct):  # 0-100 -> R gain 0-255
        self._visca(0x81, 0x01, 0x04, 0x43, *self._nib2(pct / 100.0 * 255), 0xFF)

    def set_ganho_azul(self, pct):      # 0-100 -> B gain 0-255
        self._visca(0x81, 0x01, 0x04, 0x44, *self._nib2(pct / 100.0 * 255), 0xFF)

    # --- Exposição ---------------------------------------------------------
    def set_exposicao_modo(self, modo):
        # 0=Auto 3=Manual A=Prio.Obturador B=Prio.Íris D=Brilho
        m = {"auto": 0x00, "manual": 0x03, "shutter": 0x0A,
             "iris": 0x0B, "bright": 0x0D}.get(modo, 0x00)
        self._visca(0x81, 0x01, 0x04, 0x39, m, 0xFF)

    # --- Íris (abertura), Shutter (velocidade) e Brilho: passo a passo -----
    # (funcionam com a Exposição em Manual / prioridade correspondente)
    def iris_mais(self):    # abre a lente (mais claro)
        self._visca(0x81, 0x01, 0x04, 0x0B, 0x02, 0xFF)

    def iris_menos(self):   # fecha a lente (mais escuro)
        self._visca(0x81, 0x01, 0x04, 0x0B, 0x03, 0xFF)

    def shutter_mais(self):  # obturador mais rápido
        self._visca(0x81, 0x01, 0x04, 0x0A, 0x02, 0xFF)

    def shutter_menos(self):  # obturador mais lento
        self._visca(0x81, 0x01, 0x04, 0x0A, 0x03, 0xFF)

    def brilho_mais(self):
        self._visca(0x81, 0x01, 0x04, 0x0D, 0x02, 0xFF)

    def brilho_menos(self):
        self._visca(0x81, 0x01, 0x04, 0x0D, 0x03, 0xFF)

    # --- Menu interno (OSD) ------------------------------------------------
    def _visca_hex(self, texto):
        try:
            self._visca(*[int(x, 16) for x in texto.split()])
        except Exception as e:
            print("Aviso: comando OSD inválido:", e)

    def osd_menu(self):
        self._visca_hex(self.osd_cmd_menu)

    def osd_ok(self):
        self._visca_hex(self.osd_cmd_ok)

    def osd_voltar(self):
        self._visca_hex(self.osd_cmd_voltar)

    def osd_seta(self, pan, tilt):
        """Um 'toque' de navegação no menu (move um pouco e para)."""
        v = self.osd_vel_seta
        try:
            self._enviar_visca(pan * v, tilt * v, 0)
            time.sleep(0.15)
        finally:
            self._enviar_visca(0, 0, 0)

    # --- Foco --------------------------------------------------------------
    def set_foco_auto(self, auto):
        # 0x02 = automático, 0x03 = manual
        self._visca(0x81, 0x01, 0x04, 0x38, 0x02 if auto else 0x03, 0xFF)

    def foco_onepush(self):
        # foca uma vez automaticamente (mesmo em modo manual)
        self._visca(0x81, 0x01, 0x04, 0x18, 0x01, 0xFF)

    def foco_perto(self, ligar, vel=2):
        # velocidade variável (0-7). Menor = foco mais devagar / menos sensível.
        self._visca(0x81, 0x01, 0x04, 0x08, (0x20 | (vel & 0x07)) if ligar else 0x00, 0xFF)

    def foco_longe(self, ligar, vel=2):
        self._visca(0x81, 0x01, 0x04, 0x08, (0x30 | (vel & 0x07)) if ligar else 0x00, 0xFF)

    def foco_parar(self):
        # STOP do foco enviado 3x — garante que pare mesmo perdendo pacote UDP
        # (evita o foco "disparar" e correr sozinho depois de um pulso curto).
        for _ in range(3):
            self._visca(0x81, 0x01, 0x04, 0x08, 0x00, 0xFF)

    def set_af_sensibilidade(self, baixa):
        # 0x03 = baixa (não caça o telão), 0x02 = normal
        self._visca(0x81, 0x01, 0x04, 0x58, 0x03 if baixa else 0x02, 0xFF)

    def focar_e_travar(self):
        # foca UMA vez (no que está no centro/enquadrado) e trava em manual,
        # para não ficar caçando o telão de LED ao fundo.
        self._visca(0x81, 0x01, 0x04, 0x38, 0x03, 0xFF)   # foco manual
        self._visca(0x81, 0x01, 0x04, 0x18, 0x01, 0xFF)   # one-push AF (foca uma vez)

    # --- LEITURA das configurações atuais da câmera (consultas VISCA) ------
    @staticmethod
    def _payload(data):
        """Extrai os bytes de dados de uma resposta VISCA (entre o 50 e o FF)."""
        if not data:
            return None
        i = data.find(b"\x90\x50")
        if i >= 0:
            start = i + 2
        else:
            j = data.find(b"\x50")
            if j < 0:
                return None
            start = j + 1
        end = data.find(b"\xFF", start)
        if end < 0:
            end = len(data)
        return list(data[start:end])

    def _consultas(self, consultar):
        """Roda a lista de perguntas usando a função consultar(*bytes)->payload."""
        d = {}

        def escala(nibbles, maxraw):
            if not nibbles:
                return None
            if len(nibbles) >= 2:
                val = ((nibbles[-2] & 0x0F) << 4) | (nibbles[-1] & 0x0F)
            else:
                val = nibbles[-1] & 0x0F
            return max(0, min(100, round(val / float(maxraw) * 100)))

        p = consultar(0x81, 0x09, 0x04, 0x35, 0xFF)           # White Balance
        if p:
            code = p[0]
            if code == self.wb_var_codigo:
                d["wb"] = "var"
            else:
                d["wb"] = {0: "auto", 1: "indoor", 2: "outdoor",
                           3: "onepush", 5: "manual"}.get(code & 0x0F, "auto")
        # cores (saturação/contraste/brilho/matiz/nitidez) vêm por HTTP (get_image_conf)
        p = consultar(0x81, 0x09, 0x04, 0x39, 0xFF)           # Exposição (AE)
        if p:
            d["exposicao"] = {0: "auto", 3: "manual", 0x0A: "shutter",
                              0x0B: "iris", 0x0D: "bright"}.get(p[0] & 0x0F, "auto")
        p = consultar(0x81, 0x09, 0x04, 0x38, 0xFF)           # Foco (auto/manual)
        if p:
            d["foco_auto"] = (p[0] & 0x0F) == 2
        p = consultar(0x81, 0x09, 0x04, 0x4A, 0xFF)           # Shutter (posição)
        if p and len(p) >= 2:
            d["shutter_pos"] = ((p[-2] & 0x0F) << 4) | (p[-1] & 0x0F)
        p = consultar(0x81, 0x09, 0x04, 0x4B, 0xFF)           # Íris (posição)
        if p and len(p) >= 2:
            d["iris_pos"] = ((p[-2] & 0x0F) << 4) | (p[-1] & 0x0F)
        return d

    def ler_configuracoes(self, timeout=0.5):
        """Lê os valores atuais da câmera, tentando vários jeitos até um responder:
        VISCA cru (UDP 1259), Sony VISCA-over-IP com cabeçalho (UDP 52381, dois tipos
        de pacote) e VISCA por TCP (5678)."""
        metodos = [
            ("visca-udp-%d" % self.visca_porta, "udp", self.visca_porta, None),
            ("sony-52381-inq", "udp", 52381, 0x10),
            ("sony-52381-cmd", "udp", 52381, 0x00),
            ("visca-udp-52381", "udp", 52381, None),
            ("sony-1259-inq", "udp", self.visca_porta, 0x10),
            ("visca-tcp-5678", "tcp", 5678, None),
            ("sony-tcp-5678-inq", "tcp", 5678, 0x10),
        ]
        resultado = {}
        for nome, transporte, porta, htype in metodos:
            try:
                d = self._tentar_ler(transporte, porta, htype, timeout)
            except Exception:
                d = None
            if d:
                d["_modo"] = nome
                resultado = d
                break

        # cores/imagem por HTTP (bright/saturation/contrast/sharpness/hue) — sempre
        # tenta, pois é o caminho confiável e é o único que traz o contraste.
        img = self.ler_imagem_http()
        if img:
            mapa = {"saturation": "saturacao", "contrast": "contraste",
                    "bright": "brilho", "hue": "matiz", "sharpness": "nitidez",
                    "flip": "flip", "mirror": "mirror", "ldc": "ldc"}
            for k, v in img.items():
                if k in mapa:
                    resultado[mapa[k]] = v
            resultado.setdefault("_modo", "http")
            resultado["_http_img"] = True

        return resultado

    def _tentar_ler(self, transporte, porta, htype, timeout):
        import socket as _socket
        s = _socket.socket(_socket.AF_INET,
                           _socket.SOCK_DGRAM if transporte == "udp" else _socket.SOCK_STREAM)
        s.settimeout(timeout)
        seq = [0]
        try:
            if transporte == "tcp":
                s.connect((self.ip, porta))

            def enviar(pkt):
                if transporte == "udp":
                    s.sendto(pkt, (self.ip, porta))
                else:
                    s.sendall(pkt)

            # modo Sony (com cabeçalho): reseta o contador de sequência primeiro
            if htype is not None:
                try:
                    enviar(bytes([0x02, 0x00, 0x00, 0x01, 0, 0, 0, 0, 0x01]))
                    s.settimeout(0.2)
                    try:
                        (s.recvfrom(32) if transporte == "udp" else s.recv(32))
                    except Exception:
                        pass
                    s.settimeout(timeout)
                except Exception:
                    pass

            def consultar(*b):
                payload = bytes(b)
                try:
                    if htype is None:
                        enviar(payload)
                    else:
                        seq[0] += 1
                        hdr = bytes([0x01, htype, (len(payload) >> 8) & 0xFF, len(payload) & 0xFF]) \
                            + seq[0].to_bytes(4, "big")
                        enviar(hdr + payload)
                    data = s.recvfrom(64)[0] if transporte == "udp" else s.recv(64)
                    if htype is not None and len(data) >= 8:
                        data = data[8:]
                    return self._payload(data)
                except Exception:
                    return None

            # batidinha: se a 1ª pergunta não responder, este método não serve
            if consultar(0x81, 0x09, 0x04, 0x35, 0xFF) is None:
                return None
            return self._consultas(consultar)
        finally:
            try:
                s.close()
            except Exception:
                pass

    # --- API pública -------------------------------------------------------
    def mover(self, pan, tilt, zoom=0):
        """pan/tilt/zoom são inteiros com sinal.
        pan:  + direita, - esquerda   (0 = parado)
        tilt: + cima,    - baixo       (0 = parado)
        zoom: + aproxima, - afasta     (0 = parado)
        """
        if self.inverter_pan:
            pan = -pan
        if self.inverter_tilt:
            tilt = -tilt

        cmd = (int(pan), int(tilt), int(zoom))
        agora = time.time()
        # Só reenvia se o comando mudou ou passou tempo suficiente (refresh).
        if cmd == self._ultimo_cmd and (agora - self._ultimo_envio) < 0.5:
            return
        self._ultimo_cmd = cmd
        self._ultimo_envio = agora

        try:
            if self.protocolo == "visca":
                self._enviar_visca(*cmd)
            else:
                self._enviar_http(*cmd)
        except Exception as e:
            # Não deixa o app cair por causa da rede; só avisa de vez em quando.
            print("Aviso: falha ao enviar comando à câmera:", e)

    def parar(self, force=False):
        if not force:
            # Usa o mover() com deduplicação: manda o stop uma vez e reforça a
            # cada 0,5 s. Assim não floodamos a câmera enquanto está parada.
            self.mover(0, 0, 0)
            return
        # force=True (ex.: ao sair): garante a parada mandando algumas vezes.
        try:
            for _ in range(2):
                if self.protocolo == "visca":
                    self._enviar_visca(0, 0, 0)
                else:
                    self._enviar_http(0, 0, 0)
        except Exception:
            pass
        self._ultimo_cmd = (0, 0, 0)
        self._ultimo_envio = time.time()

    def fechar(self):
        self.parar(force=True)
        if self._sock:
            self._sock.close()

    # --- Presets (a câmera guarda a posição) ------------------------------
    def salvar_preset(self, n):
        # Salva posição+zoom+foco no preset da câmera. Sempre por VISCA (canal
        # confirmado), enviado 3x porque UDP pode perder pacote (preset é one-shot).
        n = max(0, min(255, int(n)))
        pkt = bytes([0x81, 0x01, 0x04, 0x3F, 0x01, n, 0xFF])
        for _ in range(3):
            try:
                self._sock.sendto(pkt, (self.ip, self.visca_porta))
                time.sleep(0.04)
            except Exception as e:
                print("Aviso: falha ao salvar preset:", e)
        if self.protocolo != "visca":
            try:
                requests.get("http://%s:%d/cgi-bin/ptzctrl.cgi?ptzcmd&posset&%d"
                             % (self.ip, self.http_porta, n), timeout=0.5)
            except Exception:
                pass

    def chamar_preset(self, n):
        n = max(0, min(255, int(n)))
        pkt = bytes([0x81, 0x01, 0x04, 0x3F, 0x02, n, 0xFF])
        for _ in range(3):
            try:
                self._sock.sendto(pkt, (self.ip, self.visca_porta))
                time.sleep(0.04)
            except Exception as e:
                print("Aviso: falha ao chamar preset:", e)
        if self.protocolo != "visca":
            try:
                requests.get("http://%s:%d/cgi-bin/ptzctrl.cgi?ptzcmd&poscall&%d"
                             % (self.ip, self.http_porta, n), timeout=0.5)
            except Exception:
                pass

    # --- VISCA sobre IP ----------------------------------------------------
    def _enviar_visca(self, pan, tilt, zoom):
        # Pan/Tilt
        pan_speed = max(1, min(24, abs(pan)))
        tilt_speed = max(1, min(20, abs(tilt)))
        p = 0x03 if pan == 0 else (0x02 if pan > 0 else 0x01)   # 1=esq, 2=dir, 3=parado
        t = 0x03 if tilt == 0 else (0x01 if tilt > 0 else 0x02)  # 1=cima, 2=baixo, 3=parado
        pkt = bytes([0x81, 0x01, 0x06, 0x01, pan_speed, tilt_speed, p, t, 0xFF])
        self._sock.sendto(pkt, (self.ip, self.visca_porta))

        # Zoom
        if zoom == 0:
            zpkt = bytes([0x81, 0x01, 0x04, 0x07, 0x00, 0xFF])
        elif zoom > 0:
            zspeed = max(0, min(7, abs(zoom)))
            zpkt = bytes([0x81, 0x01, 0x04, 0x07, 0x20 | zspeed, 0xFF])
        else:
            zspeed = max(0, min(7, abs(zoom)))
            zpkt = bytes([0x81, 0x01, 0x04, 0x07, 0x30 | zspeed, 0xFF])
        self._sock.sendto(zpkt, (self.ip, self.visca_porta))

    # --- HTTP CGI (PTZOptics) ---------------------------------------------
    def _enviar_http(self, pan, tilt, zoom):
        base = "http://%s:%d/cgi-bin/ptzctrl.cgi?ptzcmd" % (self.ip, self.http_porta)
        pan_speed = max(1, min(24, abs(pan))) if pan else 1
        tilt_speed = max(1, min(20, abs(tilt))) if tilt else 1

        # Direção combinada (diagonais suportadas)
        if pan == 0 and tilt == 0:
            direcao = "ptzstop"
        elif pan > 0 and tilt == 0:
            direcao = "right"
        elif pan < 0 and tilt == 0:
            direcao = "left"
        elif pan == 0 and tilt > 0:
            direcao = "up"
        elif pan == 0 and tilt < 0:
            direcao = "down"
        elif pan > 0 and tilt > 0:
            direcao = "rightup"
        elif pan > 0 and tilt < 0:
            direcao = "rightdown"
        elif pan < 0 and tilt > 0:
            direcao = "leftup"
        else:
            direcao = "leftdown"

        requests.get("%s&%s&%d&%d" % (base, direcao, pan_speed, tilt_speed), timeout=0.4)

        if zoom == 0:
            requests.get("%s&zoomstop&0&0" % base, timeout=0.4)
        elif zoom > 0:
            requests.get("%s&zoomin&%d&0" % (base, max(1, min(7, abs(zoom)))), timeout=0.4)
        else:
            requests.get("%s&zoomout&%d&0" % (base, max(1, min(7, abs(zoom)))), timeout=0.4)


# ----------------------------------------------------------------------------
# Leitor de vídeo em thread (mantém sempre o quadro mais recente, sem atraso)
# ----------------------------------------------------------------------------
class LeitorVideo:
    def __init__(self, url):
        self.url = url
        self.cap = None
        self.frame = None
        self.rodando = False
        self.lock = threading.Lock()
        self.thread = None

    def iniciar(self):
        self.rodando = True
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()
        return self

    def _abrir(self):
        # FFMPEG + TCP costuma ser mais estável para RTSP.
        os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp"
        cap = cv2.VideoCapture(self.url, cv2.CAP_FFMPEG)
        try:
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except Exception:
            pass
        return cap

    def _loop(self):
        while self.rodando:
            if self.cap is None or not self.cap.isOpened():
                self.cap = self._abrir()
                if not self.cap.isOpened():
                    print("Aviso: não consegui abrir o vídeo. Tentando de novo...")
                    time.sleep(1.5)
                    continue
            ok, frame = self.cap.read()
            if not ok:
                self.cap.release()
                self.cap = None
                time.sleep(0.5)
                continue
            with self.lock:
                self.frame = frame

    def ler(self):
        with self.lock:
            return None if self.frame is None else self.frame.copy()

    def parar(self):
        self.rodando = False
        if self.thread:
            self.thread.join(timeout=1.0)
        if self.cap:
            self.cap.release()


# ----------------------------------------------------------------------------
# Aplicativo principal
# ----------------------------------------------------------------------------
class App:
    def __init__(self):
        self.cfg = carregar_config()
        self.proc_w = self.cfg.getint("rastreamento", "largura_processamento", fallback=960)
        self.zona_x = self.cfg.getfloat("rastreamento", "zona_morta_x", fallback=0.06)
        self.zona_y = self.cfg.getfloat("rastreamento", "zona_morta_y", fallback=0.06)
        self.ganho_pan = self.cfg.getfloat("rastreamento", "ganho_pan", fallback=14.0)
        self.ganho_tilt = self.cfg.getfloat("rastreamento", "ganho_tilt", fallback=12.0)
        self.vmax_pan = self.cfg.getint("rastreamento", "vel_max_pan", fallback=12)
        self.vmax_tilt = self.cfg.getint("rastreamento", "vel_max_tilt", fallback=10)
        self.mira_v = self.cfg.getfloat("rastreamento", "mira_vertical", fallback=0.30)
        self.tela_x = self.cfg.getfloat("rastreamento", "posicao_tela_x", fallback=0.50)
        self.tela_y = self.cfg.getfloat("rastreamento", "posicao_tela_y", fallback=0.38)
        self.zoom_auto = self.cfg.getboolean("rastreamento", "zoom_automatico", fallback=False)
        self.zoom_alvo = self.cfg.getfloat("rastreamento", "zoom_alvo_altura", fallback=0.70)
        self.timeout_perda = self.cfg.getfloat("rastreamento", "timeout_perda", fallback=3.0)
        self.modelo_nome = self.cfg.get("rastreamento", "modelo", fallback="yolov8n.pt")

        self.camera = ControladorCamera(self.cfg)
        self.video = LeitorVideo(self.cfg.get("camera", "rtsp_url"))

        self.raio_reid = self.cfg.getfloat("rastreamento", "raio_reidentificacao", fallback=0.18)

        self.vman_pan = self.cfg.getint("manual", "vel_pan", fallback=10)
        self.vman_tilt = self.cfg.getint("manual", "vel_tilt", fallback=8)
        self.vman_zoom = self.cfg.getint("manual", "vel_zoom", fallback=3)

        self.alvo_id = None          # ID atual da pessoa que estamos seguindo
        self.alvo_box = None         # última caixa conhecida do alvo (x1,y1,x2,y2)
        self.pausado = False
        self.clique = None           # (x, y) do último clique
        self.ultimo_visto = 0.0      # quando vimos o alvo pela última vez
        self.escala_mult = 1.0       # multiplicador de sensibilidade (+/-)
        self._ultimas_caixas = []    # detecções do último quadro (reuso no modo manual)
        self.salvar_preset_ate = 0.0 # modo "salvar preset" ativo até este instante
        self.msg = ""                # mensagem temporária na tela
        self.msg_ate = 0.0

        self.janela = "Rastreador PTZ - clique na pessoa para seguir"

    # --- callback do mouse -------------------------------------------------
    def _on_mouse(self, event, x, y, flags, param):
        if event == cv2.EVENT_LBUTTONDOWN:
            self.clique = (x, y)

    # --- laço principal ----------------------------------------------------
    def rodar(self):
        print("Carregando modelo de IA (%s)... na primeira vez pode baixar." % self.modelo_nome)
        from ultralytics import YOLO
        modelo = YOLO(caminho_modelo(self.modelo_nome))
        print("Modelo carregado. Abrindo vídeo...")

        self.video.iniciar()
        cv2.namedWindow(self.janela, cv2.WINDOW_NORMAL)
        cv2.setMouseCallback(self.janela, self._on_mouse)

        # espera o primeiro quadro
        t0 = time.time()
        while self.video.ler() is None:
            if time.time() - t0 > 15:
                print("ERRO: não recebi vídeo da câmera. Verifique o IP/RTSP no config.ini.")
                self._encerrar()
                return
            time.sleep(0.2)

        fps_t = time.time()
        fps = 0.0

        try:
            while True:
                frame = self.video.ler()
                if frame is None:
                    time.sleep(0.01)
                    continue

                # redimensiona para processar (clique e detecção no mesmo tamanho)
                h0, w0 = frame.shape[:2]
                escala = self.proc_w / float(w0)
                frame = cv2.resize(frame, (self.proc_w, int(h0 * escala)))
                H, W = frame.shape[:2]

                # --- lê o teclado manual (estado real das teclas) ---
                man_pan = man_tilt = man_zoom = 0
                if _janela_em_foco(self.janela):
                    if _tecla_pressionada("D"):
                        man_pan += self.vman_pan
                    if _tecla_pressionada("A"):
                        man_pan -= self.vman_pan
                    if _tecla_pressionada("W"):
                        man_tilt += self.vman_tilt
                    if _tecla_pressionada("S"):
                        man_tilt -= self.vman_tilt
                    if _tecla_pressionada("Q"):
                        man_zoom += self.vman_zoom
                    if _tecla_pressionada("E"):
                        man_zoom -= self.vman_zoom
                manual_ativo = bool(man_pan or man_tilt or man_zoom)

                # Enquanto dirige manual, pula a IA (laço bem mais rápido/responsivo).
                if manual_ativo:
                    caixas = self._ultimas_caixas
                else:
                    # detecção + rastreamento (somente pessoas = classe 0)
                    res = modelo.track(frame, persist=True, classes=[0],
                                       tracker="bytetrack.yaml", verbose=False)
                    caixas = []  # (id, x1, y1, x2, y2)
                    if res and res[0].boxes is not None and res[0].boxes.id is not None:
                        ids = res[0].boxes.id.cpu().numpy().astype(int)
                        xyxy = res[0].boxes.xyxy.cpu().numpy()
                        for i, box in enumerate(xyxy):
                            x1, y1, x2, y2 = box
                            caixas.append((int(ids[i]), int(x1), int(y1), int(x2), int(y2)))
                    self._ultimas_caixas = caixas

                # processa clique de seleção
                if self.clique is not None:
                    cx, cy = self.clique
                    self.clique = None
                    escolhido = self._pessoa_no_ponto(caixas, cx, cy)
                    if escolhido is not None:
                        self.alvo_id = escolhido
                        self.alvo_box = next((c[1:] for c in caixas if c[0] == escolhido), None)
                        self.ultimo_visto = time.time()

                # localiza o alvo: 1) pelo número; 2) se o número mudou,
                # re-gruda na detecção mais próxima de onde o alvo estava.
                alvo = self._localizar_alvo(caixas, W, H)

                pan = tilt = zoom = 0
                agora = time.time()

                if manual_ativo:
                    # controle manual pelo teclado tem prioridade sobre o rastreamento
                    pan, tilt, zoom = man_pan, man_tilt, man_zoom
                    self.camera.mover(pan, tilt, zoom)
                elif alvo is not None and not self.pausado:
                    self.alvo_id = alvo[0]
                    self.alvo_box = alvo[1:]
                    self.ultimo_visto = agora
                    pan, tilt, zoom = self._calcular_movimento(alvo, W, H)
                    self.camera.mover(pan, tilt, zoom)
                else:
                    # sem alvo visível ou pausado: câmera parada
                    self.camera.parar()
                    if alvo is None and self.alvo_id is not None:
                        if agora - self.ultimo_visto > self.timeout_perda:
                            self.alvo_id = None   # perdeu de vez; espera novo clique
                            self.alvo_box = None

                # ---- desenho na tela ----
                self._desenhar(frame, caixas, alvo, pan, tilt, zoom, fps, manual_ativo)

                # fps
                agora = time.time()
                dt = agora - fps_t
                fps_t = agora
                if dt > 0:
                    fps = 0.9 * fps + 0.1 * (1.0 / dt)

                cv2.imshow(self.janela, frame)
                k = cv2.waitKey(1) & 0xFF
                if k == 27:                    # ESC = sair
                    break
                elif k == ord(" "):
                    self.pausado = not self.pausado
                elif k in (ord("c"), ord("C")):
                    self.alvo_id = None
                    self.alvo_box = None
                elif k in (ord("n"), ord("N")):
                    self._proxima_pessoa(caixas)
                elif k in (ord("+"), ord("=")):
                    self.escala_mult = min(3.0, self.escala_mult + 0.1)
                elif k in (ord("-"), ord("_")):
                    self.escala_mult = max(0.3, self.escala_mult - 0.1)
                # (W/A/S/D e Q/E são lidos em tempo real, não aqui)
                # --- presets ---
                elif k in (ord("p"), ord("P")):
                    self.salvar_preset_ate = time.time() + 4.0
                    self._aviso("SALVAR PRESET: aperte um numero de 0 a 9")
                elif ord("0") <= k <= ord("9"):
                    n = k - ord("0")
                    if time.time() < self.salvar_preset_ate:
                        self.camera.salvar_preset(n)
                        self.salvar_preset_ate = 0.0
                        self._aviso("Preset %d salvo" % n)
                    else:
                        self.camera.chamar_preset(n)
                        self.alvo_id = None   # ao ir para um preset, solta o alvo
                        self.alvo_box = None
                        self._aviso("Chamando preset %d" % n)

                # se a janela foi fechada no X
                if cv2.getWindowProperty(self.janela, cv2.WND_PROP_VISIBLE) < 1:
                    break
        finally:
            self._encerrar()

    # --- cálculo do movimento ---------------------------------------------
    def _calcular_movimento(self, alvo, W, H):
        _id, x1, y1, x2, y2 = alvo
        # ponto de mira: centro horizontal, e "mira_vertical" da altura da pessoa
        tx = (x1 + x2) / 2.0
        ty = y1 + (y2 - y1) * self.mira_v

        # posição desejada NA TELA (não precisa ser o centro exato)
        alvo_tela_x = self.tela_x * W
        alvo_tela_y = self.tela_y * H

        # erro normalizado (-1 a 1)
        err_x = (tx - alvo_tela_x) / (W / 2.0)
        err_y = (ty - alvo_tela_y) / (H / 2.0)

        # zona morta
        if abs(err_x) < self.zona_x:
            err_x = 0.0
        if abs(err_y) < self.zona_y:
            err_y = 0.0

        pan = self.ganho_pan * self.escala_mult * err_x
        tilt = -self.ganho_tilt * self.escala_mult * err_y  # tela: y cresce pra baixo

        pan = int(max(-self.vmax_pan, min(self.vmax_pan, round(pan))))
        tilt = int(max(-self.vmax_tilt, min(self.vmax_tilt, round(tilt))))

        zoom = 0
        if self.zoom_auto:
            altura_frac = (y2 - y1) / float(H)
            if altura_frac < self.zoom_alvo - 0.08:
                zoom = 2
            elif altura_frac > self.zoom_alvo + 0.08:
                zoom = -2

        return pan, tilt, zoom

    # --- localização do alvo (com re-identificação por posição) -----------
    def _localizar_alvo(self, caixas, W, H):
        if self.alvo_id is None:
            return None

        # 1) o número ainda existe? usa direto.
        for c in caixas:
            if c[0] == self.alvo_id:
                return c

        # 2) o número mudou (o rastreador re-etiquetou). Procura a caixa mais
        #    próxima de onde o alvo estava, com tamanho parecido.
        if self.alvo_box is None:
            return None
        ax1, ay1, ax2, ay2 = self.alvo_box
        acx, acy = (ax1 + ax2) / 2.0, (ay1 + ay2) / 2.0
        ah = max(1.0, ay2 - ay1)

        raio = self.raio_reid * W
        melhor = None
        melhor_dist = None
        for c in caixas:
            _id, x1, y1, x2, y2 = c
            cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
            dist = ((cx - acx) ** 2 + (cy - acy) ** 2) ** 0.5
            if dist > raio:
                continue
            # tamanho tem que ser parecido (metade a dobro da altura anterior)
            razao = (y2 - y1) / ah
            if razao < 0.5 or razao > 2.0:
                continue
            if melhor_dist is None or dist < melhor_dist:
                melhor_dist = dist
                melhor = c
        return melhor

    # --- utilidades de seleção --------------------------------------------
    def _pessoa_no_ponto(self, caixas, x, y):
        melhor = None
        melhor_area = None
        for c in caixas:
            _id, x1, y1, x2, y2 = c
            if x1 <= x <= x2 and y1 <= y <= y2:
                area = (x2 - x1) * (y2 - y1)
                if melhor_area is None or area < melhor_area:
                    melhor_area = area
                    melhor = _id
        return melhor

    def _proxima_pessoa(self, caixas):
        if not caixas:
            return
        ids = [c[0] for c in caixas]
        if self.alvo_id not in ids:
            novo = ids[0]
        else:
            i = ids.index(self.alvo_id)
            novo = ids[(i + 1) % len(ids)]
        self.alvo_id = novo
        self.alvo_box = next((c[1:] for c in caixas if c[0] == novo), None)
        self.ultimo_visto = time.time()

    def _aviso(self, texto, segundos=2.0):
        self.msg = texto
        self.msg_ate = time.time() + segundos

    # --- desenho -----------------------------------------------------------
    def _desenhar(self, frame, caixas, alvo, pan, tilt, zoom, fps, manual_ativo=False):
        H, W = frame.shape[:2]
        # mira: mostra ONDE a câmera tenta manter a pessoa (segue posicao_tela_x/y).
        # A linha horizontal marca a altura do rosto desejada.
        mx = int(self.tela_x * W)
        my = int(self.tela_y * H)
        cv2.drawMarker(frame, (mx, my), (0, 255, 255), cv2.MARKER_CROSS, 26, 1)
        cv2.line(frame, (0, my), (W, my), (0, 200, 200), 1)

        for c in caixas:
            _id, x1, y1, x2, y2 = c
            eh_alvo = (_id == self.alvo_id)
            cor = (0, 220, 0) if eh_alvo else (200, 200, 200)
            esp = 3 if eh_alvo else 1
            cv2.rectangle(frame, (x1, y1), (x2, y2), cor, esp)
            etiqueta = ("ALVO" if eh_alvo else "pessoa %d" % _id)
            cv2.putText(frame, etiqueta, (x1, max(15, y1 - 6)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, cor, 1, cv2.LINE_AA)

        # status
        if manual_ativo:
            status, cor = "MANUAL (teclado)", (255, 200, 0)
        elif self.pausado:
            status, cor = "PAUSADO", (0, 165, 255)
        elif alvo is not None:
            status, cor = "SEGUINDO", (0, 220, 0)
        elif self.alvo_id is not None:
            status, cor = "PROCURANDO ALVO...", (0, 165, 255)
        else:
            status, cor = "CLIQUE NUMA PESSOA", (0, 200, 255)

        barra = frame[:34].copy()
        cv2.rectangle(frame, (0, 0), (W, 34), (30, 30, 30), -1)
        cv2.addWeighted(barra, 0.25, frame[:34], 0.75, 0, frame[:34])
        cv2.putText(frame, status, (10, 23), cv2.FONT_HERSHEY_SIMPLEX,
                    0.7, cor, 2, cv2.LINE_AA)
        info = "pan %+d  tilt %+d  zoom %+d   sens %.1fx   %.0f fps" % (
            pan, tilt, zoom, self.escala_mult, fps)
        cv2.putText(frame, info, (W - 430, 23), cv2.FONT_HERSHEY_SIMPLEX,
                    0.5, (220, 220, 220), 1, cv2.LINE_AA)

        # mensagem temporária (preset salvo/chamado, etc.)
        if time.time() < self.msg_ate and self.msg:
            (tw, th), _ = cv2.getTextSize(self.msg, cv2.FONT_HERSHEY_SIMPLEX, 0.8, 2)
            cv2.rectangle(frame, (W // 2 - tw // 2 - 12, H // 2 - th - 12),
                          (W // 2 + tw // 2 + 12, H // 2 + 12), (0, 0, 0), -1)
            cv2.putText(frame, self.msg, (W // 2 - tw // 2, H // 2),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 255), 2, cv2.LINE_AA)

        ajuda1 = "clique=seguir  N=proxima  C=cancela  ESPACO=pausa  +/-=sens  ESC=sair"
        ajuda2 = "MANUAL: W/A/S/D move  Q/E zoom   |   PRESETS: 0-9 chama  P depois numero salva"
        cv2.putText(frame, ajuda1, (10, H - 34), cv2.FONT_HERSHEY_SIMPLEX,
                    0.5, (180, 180, 180), 1, cv2.LINE_AA)
        cv2.putText(frame, ajuda2, (10, H - 12), cv2.FONT_HERSHEY_SIMPLEX,
                    0.5, (180, 180, 180), 1, cv2.LINE_AA)

    def _encerrar(self):
        print("Encerrando... parando a câmera.")
        try:
            self.camera.fechar()
        except Exception:
            pass
        try:
            self.video.parar()
        except Exception:
            pass
        cv2.destroyAllWindows()


if __name__ == "__main__":
    App().rodar()
