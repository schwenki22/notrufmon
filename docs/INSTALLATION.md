# notrufmon – Installation (Laborstand)

Stand: Prototyp für die Laborphase. Getestet ist der komplette Ablauf auf einem
Rechner (Probe → Terminierung → Bewertung → Weboberfläche). Echte Netze, das
LTE-Gateway und die Umleitung der Leitstelle folgen im Labor.

## Server (Ubuntu 24.04, Docker)

```bash
git clone git@github.com:schwenki22/notrufmon.git /opt/notrufmon
```

```bash
cd /opt/notrufmon/deploy/server && cp .env.example .env && cp config.example.yaml config.yaml
```

- `.env`: Domain, Datenbank-Passwort, Benutzer und Passwort-Hash für die Weboberfläche.
- `config.yaml`: unter `termination:` die Adresse(n), von denen die umgeleiteten
  Anrufe kommen (`peer`, `allowed_sources`), dazu Prüfintervall und Gesprächsdauer.
- Zertifikat als `certs/fullchain.pem` und `certs/privkey.pem`.

```bash
sudo chgrp 991 config.yaml && chmod 640 config.yaml && docker compose up -d --build
```

Firewall der VM: TCP 443 (Probes, Browser); von den `allowed_sources` UDP 5060
(SIP) und UDP 21000–21999 (RTP). Die Datenbank läuft auf Port 5433, damit sie
neben einer voipmon-Installation auf derselben VM laufen kann.

Weboberfläche: `https://<domain>/admin/`

## Standort und Leitungen anlegen

1. *Neuer Standort* → Token und Installationsbefehl notieren (nur einmal angezeigt).
2. Beim Standort *Leitung hinzufügen*, je Netz eine:
   - **Mobilfunk:** SIP-Server = IP-Adresse des Yeastar-Gateways, Benutzer/Passwort
     des SIP-Kontos auf dem Gateway, das auf den GSM-Port der jeweiligen SIM routet.
     Registriert sich die Probe nicht am Gateway (IP-Trunk), „registrieren“ abwählen.
   - **VoIP:** SIP-Server, Benutzer und Passwort des Anschlusses beim Anbieter.
   - **Testrufnummer:** die Nummer, die bei der Leitstelle ankommen muss und dort
     für die Umleitung hinterlegt ist (bei Mobilfunk die Nummer der SIM).
   - **Gewählte Nummer:** 112. Für Tests im Labor eine normale Testnummer, die auf
     den Server zeigt.

## Probe (Raspberry Pi 4, Raspberry Pi OS Lite 64-bit)

```bash
git clone https://github.com/schwenki22/notrufmon.git ~/notrufmon
```

```bash
cd ~/notrufmon && sudo deploy/probe/install.sh --server https://<domain> --token <TOKEN>
```

```bash
journalctl -u notrufmon-probe -f
```

Die Probe holt Leitungen und Zeitplan vom Server, kündigt jeden Test an, ruft an
und meldet das Ergebnis. Ohne Verbindung zum Server ruft sie **nicht** an – ein
Anruf zur 112, den das Prüfsystem nicht auswerten kann, würde nur belasten.

## Ergebnisse

| Ergebnis | Bedeutung |
|---|---|
| OK | angekommen, Rufnummer korrekt, Ton in beide Richtungen |
| Ohne Ton | angekommen, aber Ton fehlt (Richtung steht im Grund) |
| Rufnummer falsch | angekommen, aber Rufnummer fehlt oder weicht ab |
| Nicht zugestellt | abgewiesen (SIP-Code) oder kein Anruf innerhalb der Frist |
| Fehlgeleitet | angenommen, aber nicht vom Prüfsystem – möglicherweise von einem Disponenten |
| Probe gestört | Messung fehlgeschlagen; zählt nicht zur Verfügbarkeit der 112 |
