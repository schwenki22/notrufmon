# notrufmon – Verfügbarkeitsüberwachung Notruf 112

Messstellen (Raspberry Pi + Yeastar LTE-Gateway) rufen in festen Abständen aus dem
VoIP-Festnetz und den drei Mobilfunknetzen die 112 an. Die Leitstelle leitet die
bekannten Testrufnummern auf das Prüfsystem um, das jeden Anruf annimmt und bewertet:
zugestellt? Rufnummer korrekt? Ton in beide Richtungen? Fällt die Verfügbarkeit unter
einen Schwellwert, wird alarmiert.

Basis ist der SIP/RTP-Kern von voipmon (reine Python-Standardbibliothek).

## Stand

| Teil | Status |
|---|---|
| SIP-UA, RTP/RTCP (aus voipmon) | übernommen |
| Tonprüfung, die Mobilfunk-Umkodierung übersteht (`notrufmon/tones.py`) | fertig, getestet |
| Terminierung als SIP-Trunk, Zuordnung, Ergebnisklassen (`notrufmon/server/`) | fertig, lokal Ende zu Ende getestet |
| Probe-Ablauf (SIP-Leitungen) | geplant |
| Anbindung Yeastar TG200/TG400 | wenn Gerät verfügbar |
| Datenbank, Weboberfläche, Alarmierung, Berichte | aus voipmon zu übernehmen |

## Tests

```bash
python3 -m pytest -q
```
