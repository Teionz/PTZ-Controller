# -*- coding: utf-8 -*-
"""
Diagnóstico de comunicação com a câmera PTZ.

Roda sozinho: lê o IP do config.ini, testa VÁRIOS jeitos de conversar com a
câmera e mostra o que cada um respondeu. NÃO altera nada na câmera (só pergunta
o White Balance, que é uma leitura inofensiva).

Objetivo: descobrir por qual método a câmera responde, para o "Puxar
configurações" funcionar. Rode e me mande um print do resultado.
"""

import configparser
import os
import socket

WB_INQ = bytes([0x81, 0x09, 0x04, 0x35, 0xFF])   # pergunta: White Balance


def carregar_ip():
    cfg = configparser.ConfigParser()
    caminho = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.ini")
    try:
        cfg.read(caminho, encoding="utf-8")
        return cfg.get("camera", "ip", fallback="10.0.0.250")
    except Exception:
        return "10.0.0.250"


def hx(b):
    return b.hex(" ") if b else "(vazio)"


def _cabecalho_sony(payload, htype, seq=1):
    return bytes([0x01, htype, (len(payload) >> 8) & 0xFF, len(payload) & 0xFF]) \
        + seq.to_bytes(4, "big") + payload


def teste_udp(ip, porta, htype, timeout=0.8):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        if htype is not None:
            s.sendto(bytes([0x02, 0x00, 0x00, 0x01, 0, 0, 0, 0, 0x01]), (ip, porta))  # reset seq
            try:
                s.recvfrom(32)
            except Exception:
                pass
            s.sendto(_cabecalho_sony(WB_INQ, htype), (ip, porta))
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
            s.sendall(_cabecalho_sony(WB_INQ, htype))
        else:
            s.sendall(WB_INQ)
        data = s.recv(64)
        return "RESPONDEU  ->  " + hx(data)
    except socket.timeout:
        return "conectou, mas sem resposta"
    except ConnectionRefusedError:
        return "porta fechada (recusou conexão)"
    except Exception as e:
        return "erro: %s" % e
    finally:
        s.close()


def teste_http(ip):
    try:
        import requests
    except Exception:
        return "  (biblioteca 'requests' não instalada)"
    urls = [
        "http://%s/cgi-bin/param.cgi?f=get_image_conf" % ip,
        "http://%s/cgi-bin/param.cgi?get_image_conf" % ip,
        "http://%s/cgi-bin/param.cgi?post_image_value" % ip,
        "http://%s/cgi-bin/ptzctrl.cgi?ptzcmd&ptzstop&0&0" % ip,
    ]
    linhas = []
    for u in urls:
        try:
            r = requests.get(u, timeout=1.5)
            txt = r.text[:100].replace("\n", " ").replace("\r", " ")
            linhas.append("  HTTP %d  %s  ->  %s" % (r.status_code, u, txt))
        except Exception as e:
            linhas.append("  ERRO  %s  ->  %s" % (u, e))
    return "\n".join(linhas)


def main():
    ip = carregar_ip()
    print("=" * 70)
    print(" DIAGNÓSTICO DA CÂMERA PTZ   (ip = %s)" % ip)
    print("=" * 70)
    print("Pergunta usada (leitura inofensiva): White Balance = %s" % hx(WB_INQ))
    print()
    print("[1] VISCA cru   UDP  porta 1259 ......:", teste_udp(ip, 1259, None))
    print("[2] Sony(cabec) UDP  52381 tipo 0x10 .:", teste_udp(ip, 52381, 0x10))
    print("[3] Sony(cabec) UDP  52381 tipo 0x00 .:", teste_udp(ip, 52381, 0x00))
    print("[4] VISCA cru   UDP  porta 52381 .....:", teste_udp(ip, 52381, None))
    print("[5] Sony(cabec) UDP  1259  tipo 0x10 .:", teste_udp(ip, 1259, 0x10))
    print("[6] VISCA cru   TCP  porta 5678 ......:", teste_tcp(ip, 5678, None))
    print("[7] Sony(cabec) TCP  5678  tipo 0x10 .:", teste_tcp(ip, 5678, 0x10))
    print()
    print("[8] HTTP (leitura de imagem / teste de resposta):")
    print(teste_http(ip))
    print()
    print("=" * 70)
    print("  Onde aparecer 'RESPONDEU' é o método que a câmera aceita.")
    print("  Tire um PRINT desta tela e me envie.")
    print("=" * 70)


if __name__ == "__main__":
    main()
    try:
        input("\nAperte ENTER para fechar...")
    except Exception:
        pass
