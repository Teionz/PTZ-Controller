@echo off & python -x "%~f0" & echo. & pause & exit /b
# -*- coding: utf-8 -*-
# (Este arquivo e um .bat E um script Python ao mesmo tempo. A 1a linha roda o
#  Python neste proprio arquivo, pulando a 1a linha com -x. Nao precisa de .py.)
import configparser, json, os, socket

WB_INQ = bytes([0x81, 0x09, 0x04, 0x35, 0xFF])   # pergunta: White Balance


def achar_ip():
    pasta = os.path.dirname(os.path.abspath(__file__))
    # 1) estado.json (conexao configurada no app)
    try:
        with open(os.path.join(pasta, "estado.json"), encoding="utf-8") as f:
            ip = json.load(f).get("conexao", {}).get("ip")
            if ip:
                return ip
    except Exception:
        pass
    # 2) config.ini
    try:
        cfg = configparser.ConfigParser()
        cfg.read(os.path.join(pasta, "config.ini"), encoding="utf-8")
        return cfg.get("camera", "ip", fallback="10.0.0.250")
    except Exception:
        return "10.0.0.250"


def hx(b):
    return b.hex(" ") if b else "(vazio)"


def sony(payload, htype, seq=1):
    return bytes([0x01, htype, (len(payload) >> 8) & 0xFF, len(payload) & 0xFF]) \
        + seq.to_bytes(4, "big") + payload


def teste_udp(ip, porta, htype, timeout=0.8):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        if htype is not None:
            s.sendto(bytes([0x02, 0x00, 0x00, 0x01, 0, 0, 0, 0, 0x01]), (ip, porta))
            try:
                s.recvfrom(32)
            except Exception:
                pass
            s.sendto(sony(WB_INQ, htype), (ip, porta))
        else:
            s.sendto(WB_INQ, (ip, porta))
        data, _ = s.recvfrom(64)
        return "RESPONDEU  ->  " + hx(data)
    except socket.timeout:
        return "sem resposta (timeout)"
    except Exception as e:
        return "erro: %s" % e
    finally:
        s.close()


def teste_tcp(ip, porta, htype, timeout=0.8):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((ip, porta))
        if htype is not None:
            s.sendall(bytes([0x02, 0x00, 0x00, 0x01, 0, 0, 0, 0, 0x01]))
            try:
                s.recv(32)
            except Exception:
                pass
            s.sendall(sony(WB_INQ, htype))
        else:
            s.sendall(WB_INQ)
        return "RESPONDEU  ->  " + hx(s.recv(64))
    except socket.timeout:
        return "conectou, mas sem resposta"
    except ConnectionRefusedError:
        return "porta fechada (recusou)"
    except Exception as e:
        return "erro: %s" % e
    finally:
        s.close()


def teste_http(ip):
    try:
        import requests
    except Exception:
        return "  (biblioteca 'requests' nao instalada)"
    urls = [
        "http://%s/cgi-bin/param.cgi?get_image_conf" % ip,
        "http://%s/cgi-bin/param.cgi?get_exposure_conf" % ip,
        "http://%s/cgi-bin/param.cgi?get_exp_conf" % ip,
    ]
    out = []
    for u in urls:
        try:
            r = requests.get(u, timeout=1.5)
            txt = r.text[:160].replace("\n", " ").replace("\r", " ")
            out.append("  HTTP %d  %s  ->  %s" % (r.status_code, u, txt))
        except Exception as e:
            out.append("  ERRO  %s  ->  %s" % (u, e))
    return "\n".join(out)


def ler_pos_visca(ip):
    """Le a posicao (numero) de iris e shutter por VISCA cru na 1259."""
    out = []
    for nome, alvo in (("iris", 0x4B), ("shutter", 0x4A)):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.8)
        try:
            s.sendto(bytes([0x81, 0x09, 0x04, alvo, 0xFF]), (ip, 1259))
            data = s.recvfrom(64)[0]
            out.append("  %-8s -> resposta %s" % (nome, hx(data)))
        except Exception as e:
            out.append("  %-8s -> %s" % (nome, e))
        finally:
            s.close()
    return "\n".join(out)


def main():
    ip = achar_ip()
    print("=" * 70)
    print(" DIAGNOSTICO DA CAMERA PTZ   (ip = %s)" % ip)
    print("=" * 70)
    print("Leitura inofensiva (White Balance) = %s" % hx(WB_INQ))
    print()
    print("[1] VISCA cru   UDP  1259 ............:", teste_udp(ip, 1259, None))
    print("[2] Sony(cabec) UDP  52381 tipo 0x10 .:", teste_udp(ip, 52381, 0x10))
    print("[3] Sony(cabec) UDP  52381 tipo 0x00 .:", teste_udp(ip, 52381, 0x00))
    print("[4] VISCA cru   UDP  52381 ...........:", teste_udp(ip, 52381, None))
    print("[5] Sony(cabec) UDP  1259  tipo 0x10 .:", teste_udp(ip, 1259, 0x10))
    print("[6] VISCA cru   TCP  5678 ............:", teste_tcp(ip, 5678, None))
    print("[7] Sony(cabec) TCP  5678  tipo 0x10 .:", teste_tcp(ip, 5678, 0x10))
    print()
    print("[8] HTTP (imagem / exposicao):")
    print(teste_http(ip))
    print()
    print("[9] Posicao (numero) de IRIS e SHUTTER por VISCA:")
    print(ler_pos_visca(ip))
    print()
    print("=" * 70)
    print("  Onde aparecer 'RESPONDEU' e o metodo que a camera aceita.")
    print("  Tire um PRINT desta tela e me envie (itens 8 e 9 sao os importantes agora).")
    print("=" * 70)


try:
    main()
except Exception as e:
    print("Erro no diagnostico:", e)
