# ONVIF Viewer per Raspberry Pi 4

Trasforma un Raspberry Pi 4B in un visualizzatore dedicato: all'accensione mostra a
schermo intero, sull'uscita HDMI, il video di una telecamera ONVIF. Tutto si configura
da browser: la telecamera (ricerca automatica, credenziali, profilo) e l'indirizzo IP
del Raspberry.

## Come funziona

```
 Telecamera ONVIF ──RTSP (H.264/H.265)──► mpv (decodifica HW, DRM/KMS) ──► HDMI
        ▲                                     ▲
        │ SOAP ONVIF (profili, URI RTSP)       │ legge /etc/onvif-viewer/config.json
        │                                     │ e si riavvia quando cambia
   Interfaccia web (Flask, porta 80) ─────────┘
        │
        └── NetworkManager (nmcli) ──► IP statico / DHCP del Raspberry
```

| Componente | File | Cosa fa |
|---|---|---|
| Client ONVIF | `app/onvif_client.py` | WS-Discovery, `GetProfiles`, `GetStreamUri`, snapshot. Autenticazione WS-Security digest con compensazione dell'orologio della telecamera e ripiego su HTTP Digest. |
| Player | `app/player.py` | Avvia `mpv` a schermo intero senza desktop (DRM/KMS). Se lo stream cade riprova ogni 5 s; se la telecamera non è configurata mostra a monitor l'indirizzo dell'interfaccia web. |
| Interfaccia web | `app/web.py`, `app/templates/` | Login, stato, configurazione telecamera e rete, cambio password, riavvio. |
| Rete | `app/network.py` | Legge e modifica IP, gateway e DNS di `eth0`/`wlan0` tramite NetworkManager. |
| Servizi | `systemd/`, `polkit/` | Due servizi systemd con un utente dedicato `onvifview` (niente root); una regola polkit gli consente di cambiare la rete e riavviare. |

## Requisiti

- Raspberry Pi 4B (funziona anche su Pi 5 e Pi 3B+, con prestazioni diverse).
- **Raspberry Pi OS Lite 64 bit** (Bookworm o Trixie). Non serve il desktop.
- Monitor HDMI collegato alla porta **HDMI0** (quella vicino all'alimentazione).
- Telecamera ONVIF Profile S sulla stessa rete.

## Immagine microSD pronta (consigliato)

Il modo più semplice: un'immagine con Raspberry Pi OS Lite 64 bit (senza desktop) e
ONVIF Viewer già installato.

1. **Scarica l'immagine** `onvif-viewer-pi.img.xz` dalla pagina *Releases* del repository,
   oppure da *Actions → Immagine microSD → ultima esecuzione → Artifacts*
   (l'artifact è uno zip che contiene il file `.img.xz`).
2. **Scrivila sulla microSD** con Raspberry Pi Imager: *Scegli dispositivo: Raspberry Pi 4* →
   *Scegli OS: Usa immagine personalizzata* → seleziona il file `.img.xz` → *Avanti*.
   Alla domanda sulle personalizzazioni rispondi **No**: l'immagine è già configurata.
3. **Accendi** il Raspberry con cavo di rete e monitor (porta HDMI0). Il primo avvio dura
   un minuto in più (espande il filesystem e si riavvia). Poi il monitor mostra
   l'indirizzo dell'interfaccia web.

Accessi predefiniti: interfaccia web **admin / admin**; SSH e console **pi / raspberry**
(la password di `pi` va cambiata al primo accesso). Nome host: `onvif-viewer`.

**Rigenerare l'immagine**: *Actions → Immagine microSD → Run workflow* (puoi scegliere la
password di `pi` e se attivare SSH), oppure crea un tag `v1.0.1` per avere anche una Release.
Su un PC Linux puoi crearla localmente:

```bash
sudo apt-get install -y qemu-user-static binfmt-support parted e2fsprogs xz-utils curl
sudo ./image/build-image.sh       # produce image/onvif-viewer-pi.img.xz
```

## Installazione su un Raspberry già configurato

1. Scrivi Raspberry Pi OS Lite (64 bit) sulla microSD con **Raspberry Pi Imager**.
   Nelle impostazioni dell'Imager abilita SSH e imposta utente e password.
2. Avvia il Raspberry collegato alla rete via cavo e accedi via SSH.
3. Copia questa cartella sul Raspberry (per esempio con `scp -r onvif-viewer pi@<ip>:`) e lancia:

   ```bash
   cd onvif-viewer
   sudo ./install.sh
   sudo reboot
   ```

Al riavvio il monitor mostra *"Nessuna telecamera configurata — Configura da http://…"*.

## Uso

1. Apri dal PC l'indirizzo mostrato a monitor (es. `http://192.168.1.23`).
2. Accedi con **admin / admin** e cambia subito la password nella scheda *Sistema*.
3. Scheda **Telecamera**:
   - *Cerca telecamere* trova i dispositivi ONVIF in rete; clicca quello giusto.
   - Inserisci utente e password ONVIF e premi *Connetti e leggi profili*.
   - Scegli il profilo (MainStream per la qualità piena, SubStream se la rete è lenta)
     e premi *Salva e mostra a monitor*. Il video compare in 2–3 secondi.
   - In alternativa puoi incollare direttamente un URL RTSP nella sezione
     *URL RTSP manuale*: utile per telecamere non ONVIF.
4. Scheda **Rete**: scegli l'interfaccia, *DHCP* o *IP statico* (formato
   `192.168.1.50/24`), gateway e DNS, poi *Applica*. Dopo pochi secondi riapri la pagina al
   nuovo indirizzo.
5. Scheda **Stato**: stato del player, stream in uso, temperatura, anteprima (snapshot).

## Note tecniche

- **Decodifica**: `mpv --hwdec=auto-safe` usa il decoder hardware del Pi 4 (H.264 via
  V4L2, H.265 via il decoder HEVC dedicato). Un MainStream 1080p H.264 o H.265 viene
  riprodotto con carico CPU basso. Il Pi 4 non decodifica in hardware stream 4K H.264:
  in quel caso scegli il SubStream o imposta la telecamera a 1080p.
- **Latenza**: con *Bassa latenza* attiva (profilo `low-latency` di mpv, nessuna cache) il
  ritardo tipico è 300–600 ms su TCP. Su reti affidabili UDP può ridurlo ancora.
- **Orologio**: molte telecamere rifiutano l'autenticazione se l'ora è sbagliata. Il
  client legge l'ora della telecamera e la compensa, quindi non serve sincronizzarla.
- **XAddr errati**: se la telecamera pubblica un indirizzo interno diverso da quello con
  cui la raggiungi (NAT, IP cambiato), il client usa comunque l'IP che hai inserito.
- **Sicurezza**: i servizi girano come utente `onvifview`; la configurazione (che
  contiene la password della telecamera) è in `/etc/onvif-viewer/config.json` con permessi
  `600`. L'interfaccia web è HTTP semplice: usala su rete locale fidata.
- **Console**: `install.sh` disattiva il login su `tty1` e nasconde cursore e logo, così il
  monitor mostra solo il video. Per una console locale usa `Ctrl+Alt+F2`, oppure SSH.

## Comandi utili

```bash
systemctl status onvif-player onvif-web     # stato dei servizi
journalctl -u onvif-player -f               # log del player (errori di stream)
journalctl -u onvif-web -f                  # log dell'interfaccia web
sudo systemctl restart onvif-player         # riavvia la visualizzazione
sudo cat /etc/onvif-viewer/config.json      # configurazione corrente
```

**Password web dimenticata**: rimuovi l'hash e torna ad `admin`:

```bash
sudo python3 -c "import json;p='/etc/onvif-viewer/config.json';c=json.load(open(p));c['web']['password_hash']='';json.dump(c,open(p,'w'),indent=2)"
```

**IP del Raspberry perso**: rimuovi la telecamera dalla configurazione (o scollegala) e
l'indirizzo comparirà di nuovo a monitor; oppure cercalo nella lista DHCP del router.

## Problemi frequenti

| Sintomo | Causa probabile |
|---|---|
| Schermo nero, nessun testo | Monitor sulla porta HDMI1, oppure `dtoverlay=vc4-kms-v3d` mancante in `/boot/firmware/config.txt`. Controlla `journalctl -u onvif-player`. |
| "Autenticazione rifiutata" | Utente ONVIF diverso da quello web della telecamera (su Hikvision/Dahua l'utente ONVIF va creato a parte), oppure ONVIF disattivato. |
| "Nessun dispositivo trovato" | WS-Discovery disattivato sulla telecamera o telecamera su un'altra sottorete: inserisci l'IP a mano. |
| Video a scatti | Stream 4K o bitrate molto alto: usa il SubStream o riduci risoluzione/bitrate sulla telecamera. |
| "Connessione persa" ciclico | Prova il trasporto TCP, verifica il cavo e che la telecamera accetti più client RTSP. |

## Test

I test usano una telecamera ONVIF simulata e un `nmcli` finto, quindi girano su qualsiasi PC:

```bash
python3 tests/test_all.py
```

## Disinstallazione

```bash
sudo ./uninstall.sh          # conserva la configurazione
sudo ./uninstall.sh --purge  # rimuove anche configurazione e utente
```
