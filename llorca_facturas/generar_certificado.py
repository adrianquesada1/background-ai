"""
Certificado HTTPS para servir la aplicación cifrada en la red de la oficina.

    python generar_certificado.py                  (nombre del equipo + todas sus IP)
    python generar_certificado.py --nombre llorca.miempresa.es --ip 192.168.1.20

Crea `certificados/servidor.crt` y `certificados/servidor.key` (la clave privada NO se sube a ningún sitio: está en
.gitignore). servidor.bat los detecta y arranca en https://.

Es un certificado autofirmado: el navegador avisará la primera vez. Para que no avise, instale `servidor.crt` en
«Entidades de certificación raíz de confianza» de cada equipo (o por directiva de grupo), o use un certificado de la
CA interna de la empresa colocándolo con esos mismos nombres. Para publicar la aplicación en internet, póngala detrás de
un proxy inverso con certificado público (Caddy o IIS con Let's Encrypt) y no abra el puerto 8501 directamente.
"""
from __future__ import annotations

import argparse
import datetime as dt
import ipaddress
import os
import socket
from pathlib import Path


def ips_locales() -> list[str]:
    out = {"127.0.0.1"}
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            out.add(info[4][0])
    except OSError:
        pass
    return sorted(out)


def generar(nombres: list[str], ips: list[str], carpeta: Path, dias: int = 825) -> tuple[Path, Path]:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID
    carpeta.mkdir(parents=True, exist_ok=True)
    clave = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    sujeto = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, nombres[0]),
                        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Llorca Group - Control economico de obra")])
    san = [x509.DNSName(n) for n in nombres] + [x509.IPAddress(ipaddress.ip_address(i)) for i in ips]
    ahora = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(sujeto).issuer_name(sujeto).public_key(clave.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(ahora - dt.timedelta(minutes=5))
            .not_valid_after(ahora + dt.timedelta(days=dias))
            .add_extension(x509.SubjectAlternativeName(san), critical=False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .sign(clave, hashes.SHA256()))
    crt, key = carpeta / "servidor.crt", carpeta / "servidor.key"
    crt.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key.write_bytes(clave.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                                        serialization.NoEncryption()))
    try:
        os.chmod(key, 0o600)
    except OSError:
        pass
    return crt, key


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--nombre", action="append", help="nombre DNS (se puede repetir)")
    ap.add_argument("--ip", action="append", help="dirección IP (se puede repetir)")
    ap.add_argument("--dias", type=int, default=825)
    a = ap.parse_args()
    nombres = a.nombre or [socket.gethostname(), "localhost"]
    ips = a.ip or ips_locales()
    crt, key = generar(nombres, ips, Path(__file__).resolve().parent / "certificados", a.dias)
    print(f"Certificado: {crt}\nClave privada: {key}\nVálido para: {', '.join(nombres + ips)} durante {a.dias} días.")
    print("Arranque servidor.bat: la aplicación se servirá en https://")


if __name__ == "__main__":
    main()
