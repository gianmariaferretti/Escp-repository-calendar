import os
import requests
from bs4 import BeautifulSoup
# Correzione import ICS - Percorso corretto per ContentLine
from ics import Calendar, Event
from ics.grammar.parse import ContentLine
from github import Github, GithubException, UnknownObjectException
from datetime import datetime, timezone, timedelta
from dateutil import parser as dtparser
import json
import logging
import time
# Aggiunto import per userdata
from google.colab import userdata

# Configurazione logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- Variabili di Configurazione ---
ESCP_BASE = os.getenv("ESCP_BASE", "https://ent.escp.eu")
LOGIN_URL = os.getenv("ESCP_LOGIN_URL", f"{ESCP_BASE}/login") # Non più usato con i cookie
LOGIN_POST = os.getenv("ESCP_LOGIN_POST", f"{ESCP_BASE}/login_check") # Non più usato con i cookie
# URL API corretto
CAL_API   = "https://ent.escp.eu/en/StudentDashboard/Timetable/events"

TZ_NAME   = os.getenv("CAL_TZ", "Europe/Rome") # Timezone per eventi senza tz specificato
CAL_NAME  = os.getenv("CAL_NAME", "ESCP Calendar") # Nome del calendario nel file ICS

# --- Funzioni ---

def get_credentials():
    """
    Recupera le credenziali usando il metodo userdata di Colab,
    gestendo correttamente i segreti opzionali.
    """
    try:
        from google.colab import userdata
        from google.colab.userdata import SecretNotFoundError
    except ImportError:
        # Fallback se non si è in Colab (anche se l'utente è in Colab)
        logging.warning("Non è stato possibile importare userdata da Colab, provo con os.getenv...")
        creds = {
            "github_token": os.getenv("GITHUB_TOKEN"),
            "private_repo_name": os.getenv("GITHUB_PRIVATE_REPO"),
            "email": os.getenv("ESCP_EMAIL"),
            "password": os.getenv("ESCP_PASSWORD"),
            "public_repo_name": os.getenv("GITHUB_PUBLIC_REPO"),
            "escp_cookies": os.getenv("ESCP_COOKIES")
        }
        if not all([creds["github_token"], creds["private_repo_name"]]):
             raise ValueError("ERRORE: GITHUB_TOKEN o GITHUB_PRIVATE_REPO non trovati.")
        if not ((creds.get("email") and creds.get("password")) or creds.get("escp_cookies")):
            raise ValueError("ERRORE: mancano le credenziali ESCP (email/password) o i cookie.")
        return creds

    # Logica principale per Colab
    try:
        creds = {
            "github_token": userdata.get("GITHUB_TOKEN"),
            "private_repo_name": userdata.get("GITHUB_PRIVATE_REPO"),
        }
    except SecretNotFoundError as e:
         raise ValueError(f"ERRORE: Segreto obbligatorio mancante in Colab: {e}")

    # Segreti Opzionali
    try:
        creds["email"] = userdata.get("ESCP_EMAIL")
        creds["password"] = userdata.get("ESCP_PASSWORD")
    except SecretNotFoundError:
        creds["email"] = None
        creds["password"] = None
        logging.info("Credenziali email/password ESCP non trovate, si procederà con i cookie (se presenti).")

    try:
        creds["public_repo_name"] = userdata.get("GITHUB_PUBLIC_REPO")
    except SecretNotFoundError:
        creds["public_repo_name"] = None
        logging.info("Segreto GITHUB_PUBLIC_REPO non trovato, la copia pubblica sarà saltata.")

    try:
        creds["escp_cookies"] = userdata.get("ESCP_COOKIES")
    except SecretNotFoundError:
        creds["escp_cookies"] = None
        logging.info("Segreto ESCP_COOKIES non trovato.")

    # Controllo finale: o email/pwd o cookie devono esserci
    if not ((creds.get("email") and creds.get("password")) or creds.get("escp_cookies")):
        raise ValueError("ERRORE: Devi fornire o le credenziali ESCP (EMAIL/PASSWORD) o il cookie (ESCP_COOKIES).")

    return creds

def apply_raw_cookies(session, raw_cookie_string, domain=".escp.eu"):
    """Applica una stringa di cookie raw alla sessione requests."""
    logging.info(f"Applico i cookie per il dominio: {domain}")
    try:
        pairs = raw_cookie_string.split(";")
        if not pairs:
            logging.warning("Stringa cookie vuota o mal formattata.")
            return

        for pair in pairs:
            pair = pair.strip()
            if "=" in pair:
                name, value = pair.split("=", 1)
                name = name.strip()
                value = value.strip()
                if name and value:
                    session.cookies.set(name, value, domain=domain)
                    logging.debug(f"Cookie impostato: {name}=***")
                else:
                    logging.warning(f"Ignoro coppia cookie non valida: '{pair}'")
            elif pair:
                logging.warning(f"Ignoro frammento cookie mal formattato: '{pair}'")
    except Exception as e:
        logging.error(f"Errore durante l'applicazione dei cookie: {e}")


def login_escp(session, email=None, password=None, raw_cookies=None):
    """Tenta il login, preferendo i cookie se disponibili."""
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/90.0.4430.212 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,it;q=0.8",
    })

    if raw_cookies:
        logging.info("Tentativo di autenticazione tramite cookie.")
        # Assicurati di applicare i cookie al dominio corretto!
        apply_raw_cookies(session, raw_cookies, domain=".escp.eu")

        # Testiamo l'accesso a una pagina protetta (la dashboard o la pagina del calendario API)
        test_url = f"{ESCP_BASE}/en/StudentDashboard/Timetable/events" # Usiamo l'URL API come test
        try:
            test_resp = session.get(test_url, params={"limit": 1}, timeout=30) # Chiedi solo 1 evento per testare
            logging.info(f"Test accesso con cookie a {test_url}: Status {test_resp.status_code}")
            # Considera successo se non è un redirect al login (3xx) o un errore client/server (4xx, 5xx)
            # Un 200 OK con JSON vuoto o valido va bene.
            if test_resp.ok and test_resp.headers.get('Content-Type', '').startswith('application/json'):
                 logging.info("Accesso con cookie verificato con successo.")
                 return True
            elif test_resp.status_code == 401 or test_resp.status_code == 403:
                 logging.error("Accesso negato (401/403). I cookie potrebbero essere scaduti o non validi per questa risorsa.")
                 return False
            elif "login" in test_resp.url.lower(): # Se siamo stati rediretti alla pagina di login
                 logging.error("Rediretti alla pagina di login. I cookie non sono validi.")
                 return False
            else:
                 # Potrebbe essere ok anche se non è JSON (es. pagina dashboard)
                 logging.warning(f"Risposta inattesa al test cookie (Status: {test_resp.status_code}, Content-Type: {test_resp.headers.get('Content-Type')}). Procedo comunque.")
                 return True # Proviamo ad essere ottimisti

        except requests.RequestException as e:
            logging.error(f"Errore di rete durante la verifica dei cookie: {e}")
            return False
    else:
      # Se non ci sono cookie, dichiariamo il login fallito (non supportiamo più il login via form)
      logging.error("Cookie non forniti. Il login tramite form non è più supportato per questo script.")
      return False


def fetch_calendar_events(session, days_back=30, days_forward=180):
    """
    Esegue la chiamata API con l'URL e i parametri esatti scoperti.
    """
    session.headers.update({
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": f"{ESCP_BASE}/StudentDashboard/Index/index" # Aggiungi il Referer
    })

    now = datetime.now()
    start_dt = now - timedelta(days=days_back)
    end_dt   = now + timedelta(days=days_forward)

    # Parametri ESATTI
    params = {
        "start": start_dt.strftime('%m-%d-%Y'),
        "end": end_dt.strftime('%m-%d-%Y'),
        "_dc": int(time.time() * 1000), # Anti-cache
        "page": 1,
        "limit": 999 # Limite alto
    }

    try:
        logging.info(f"Tento fetch JSON eventi con i parametri corretti: {params}")
        url_base = CAL_API.split('?')[0] # URL senza parametri query pre-esistenti
        resp = session.get(url_base, params=params, timeout=40)

        # Log più dettagliato della risposta
        logging.info(f"Risposta ricevuta da API eventi: Status {resp.status_code}, Content-Type: {resp.headers.get('Content-Type')}")

        # Controlla se la risposta è effettivamente JSON prima di provare a decodificarla
        if not resp.headers.get('Content-Type', '').startswith('application/json'):
            logging.error(f"La risposta dall'API non è JSON. Contenuto ricevuto: {resp.text[:500]}")
            # Se contiene la parola "Login", il cookie è scaduto
            if '<title>ESCP Login</title>' in resp.text or 'cas/login' in resp.text:
                 logging.error("Sembra che il cookie sia scaduto o non valido. Aggiorna il cookie ESCP_COOKIES.")
            return [] # Ritorna lista vuota perché non abbiamo dati JSON

        resp.raise_for_status() # Ora controlla errori HTTP (4xx, 5xx)

        data = resp.json()

        # Chiave corretta "evts"
        events = data.get("evts") # Non mettere 'or data' qui, vogliamo solo la lista sotto 'evts'

        # Verifica se 'events' è effettivamente una lista
        if isinstance(events, list):
            logging.info(f"Recuperati {len(events)} eventi via API con successo!")
            return events
        elif events is None:
             logging.warning("La chiave 'evts' non è presente nella risposta JSON, anche se la chiamata ha avuto successo.")
             logging.debug(f"Struttura dati ricevuta: {data}")
             return []
        else:
            logging.warning(f"La chiave 'evts' contiene dati ma non è una lista: tipo {type(events)}.")
            logging.debug(f"Dati ricevuti sotto 'evts': {events}")
            return []

    except requests.exceptions.HTTPError as http_err:
        logging.error(f"Errore HTTP durante il recupero eventi: {http_err}")
        logging.error(f"Testo della risposta (potrebbe contenere dettagli): {resp.text[:500]}")
        return []
    except requests.exceptions.ConnectionError as conn_err:
        logging.error(f"Errore di connessione durante il recupero eventi: {conn_err}")
        return []
    except requests.exceptions.Timeout as timeout_err:
        logging.error(f"Timeout durante il recupero eventi: {timeout_err}")
        return []
    except json.JSONDecodeError as json_err:
        logging.error(f"Errore nel decodificare la risposta JSON: {json_err}")
        logging.error(f"Testo ricevuto che ha causato l'errore: {resp.text[:500]}")
        return []
    except Exception as e:
        # Catch generico per altri errori imprevisti
        logging.error(f"Errore imprevisto durante il recupero eventi: {e}")
        if 'resp' in locals(): # Se abbiamo la risposta, logghiamola
            logging.error(f"Testo della risposta: {resp.text[:500]}")
        return []


def parse_dt_maybe(date_string):
    """Tenta di parsare una stringa di data in vari formati comuni."""
    if not date_string:
        return None
    try:
        # Prova il formato ISO 8601 con timezone (comune nelle API moderne)
        # Esempio: "2025-09-29T14:00:00+01:00"
        return dtparser.isoparse(date_string)
    except ValueError:
        pass # Non era nel formato ISO atteso, prova altro

    try:
        # Prova formati più generici usando dateutil.parser
        # Gestisce cose come "2025-09-29 14:00:00" ma potrebbe sbagliare la timezone
        parsed_dt = dtparser.parse(date_string)
        # Se è 'naive' (senza timezone), assumi UTC o il timezone locale? Meglio UTC.
        if parsed_dt.tzinfo is None:
             # Attenzione: Questo MARCA la data come UTC, non la CONVERTE.
             # Se la data originale era in ora locale, questo potrebbe essere sbagliato.
             # Ma per ICS è meglio avere date 'aware'.
             # Potremmo provare ad aggiungere il timezone locale qui se necessario.
             # from dateutil import tz
             # local_tz = tz.gettz(TZ_NAME) # Es: 'Europe/Rome'
             # return parsed_dt.replace(tzinfo=local_tz)
             return parsed_dt.replace(tzinfo=timezone.utc) # Più sicuro marcare UTC
        return parsed_dt # Era già 'aware'
    except Exception as e:
        logging.warning(f"Impossibile parsare la data '{date_string}': {e}")
        return None

def create_ics_file(events_data, filename="escp_calendar.ics", tz_name=TZ_NAME, cal_name=CAL_NAME):
    """Crea il file .ics dagli eventi estratti."""
    if not events_data:
        logging.warning("Nessun evento da processare; niente ICS.")
        return None

    cal = Calendar()

    # ---- CORREZIONE FINALE: USA APPEND ----
    cal.extra.append(ContentLine(name="X-WR-CALNAME", value=cal_name))
    cal.extra.append(ContentLine(name="PRODID", value="-//ESCP Sync Script//ics.tools//EN")) # Aggiornato PRODID

    count_added = 0
    skipped_count = 0
    for raw in events_data:
        try:
            title = raw.get("title", "Evento ESCP").strip()
            start_str = raw.get("start")
            end_str = raw.get("end")

            start = parse_dt_maybe(start_str)
            end = parse_dt_maybe(end_str)

            if not start:
                logging.warning(f"Evento '{title}' saltato: data di inizio mancante o non parsabile ('{start_str}').")
                skipped_count += 1
                continue

            # Se manca la fine, imposta una durata di default (es. 90 min)
            if start and not end:
                end = start + timedelta(minutes=90)
                logging.debug(f"Data fine mancante per '{title}', impostata a {end}.")

            # Se le date sono invertite, scambiale
            if start and end and end < start:
                logging.warning(f"Date invertite per '{title}', le scambio.")
                start, end = end, start

            ev = Event()
            ev.name = title
            # Assicurati che le date siano 'aware' (con timezone)
            # parse_dt_maybe dovrebbe già restituire date aware se possibile
            ev.begin = start
            ev.end = end

            # Location: prendila dal campo 'LOCATION', ripulendo se necessario
            loc = raw.get("LOCATION")
            if loc:
                # Esempio: "LONDON / LONDON / LONDON_G74 / G74" -> "G74, London"
                parts = [p.strip() for p in loc.split('/') if p.strip()]
                if len(parts) > 1:
                    room = parts[-1]
                    campus = parts[0] if parts[0].lower() != "no location data available at this time" else None
                    if room.startswith(campus): # Evita duplicati tipo "LONDON_G74"
                         room_clean = room.split('_')[-1]
                    else:
                         room_clean = room

                    if campus and campus != room_clean:
                         ev.location = f"{room_clean}, {campus.capitalize()}"
                    else:
                         ev.location = room_clean
                elif parts:
                    ev.location = parts[0] # Usa l'unica parte disponibile
                # Se non ci sono parti valide, non impostare la location

            # Description: pulisci l'HTML dal campo 'notes'
            desc_html = raw.get("notes")
            if desc_html:
                soup = BeautifulSoup(desc_html, "html.parser")
                # Estrai testo, mantieni le interruzioni di riga, rimuovi spazi extra
                lines = [line.strip() for line in soup.stripped_strings]
                ev.description = "\n".join(lines)

            # UID: usa quello fornito se disponibile, altrimenti creane uno stabile
            uid_from_data = raw.get("UID")
            if uid_from_data:
                 ev.uid = uid_from_data
            else:
                base_id = raw.get("id") or f"{start.isoformat()}_{ev.name}"
                # Usare hash non è ideale per UID se l'ID cambia, ma è meglio di niente
                ev.uid = f"escp-event-{hash(base_id)}@escp.eu" # Dominio fittizio

            ev.created = datetime.now(timezone.utc) # Data creazione evento ICS
            # ev.last_modified = datetime.now(timezone.utc) # Non strettamente necessario

            cal.events.add(ev)
            count_added += 1

        except Exception as e:
            logging.warning(f"Errore nel processare l'evento: {raw}. Dettagli: {e}")
            skipped_count += 1

    if count_added == 0:
        logging.warning(f"Nessun evento valido da scrivere nel file ICS (saltati: {skipped_count}).")
        return None

    # Salva il file
    try:
        with open(filename, "w", encoding="utf-8") as f:
            # Correzione ICS: usa .serialize()
            f.write(cal.serialize())
        logging.info(f"File ICS '{filename}' creato con successo ({count_added} eventi aggiunti, {skipped_count} saltati).")
        return filename
    except IOError as e:
        logging.error(f"Impossibile scrivere il file ICS '{filename}': {e}")
        return None


def upload_to_github(token, repo_name, file_path, commit_message, target_path=None):
    """Carica/aggiorna file in repo GitHub."""
    if not os.path.exists(file_path):
        logging.error(f"Il file '{file_path}' non trovato. Caricamento su GitHub annullato.")
        return

    try:
        g = Github(token)
        repo = g.get_repo(repo_name)
        target = target_path or os.path.basename(file_path)
        logging.info(f"Tentativo di caricare '{target}' nel repository '{repo_name}'...")

        with open(file_path, "r", encoding="utf-8") as fh:
            content = fh.read()
            # Non annullare se il file è vuoto, potremmo voler cancellare il vecchio
            # if not content:
            #      logging.warning(f"Il file '{file_path}' è vuoto. Caricamento annullato.")
            #      return

        try:
            existing = repo.get_contents(target, ref="main") # Specifica il branch 'main'
            # File esiste, aggiorna
            # Controlla se il contenuto è cambiato per evitare commit inutili
            if existing.decoded_content.decode("utf-8") == content:
                 logging.info(f"Il contenuto di '{target}' non è cambiato. Nessun aggiornamento necessario.")
            else:
                 repo.update_file(existing.path, commit_message, content, existing.sha, branch="main")
                 logging.info(f"File '{target}' aggiornato con successo nel repo '{repo_name}'.")

        except UnknownObjectException:
            # File non esiste, crea
            repo.create_file(target, commit_message, content, branch="main")
            logging.info(f"File '{target}' creato con successo nel repo '{repo_name}'.")

    except GithubException as e:
        logging.error(f"Errore GitHub ({e.status}): {e.data.get('message', 'Nessun messaggio specifico')}")
        if e.status == 401:
             logging.error("Errore di autenticazione GitHub. Controlla il GITHUB_TOKEN.")
        elif e.status == 404:
             logging.error(f"Repository '{repo_name}' non trovato o token non ha accesso. Controlla GITHUB_PRIVATE_REPO/GITHUB_PUBLIC_REPO.")
        elif e.status == 403:
             logging.error("Permessi insufficienti per scrivere nel repository o rate limit superato.")
    except Exception as e:
        logging.error(f"Errore imprevisto durante il caricamento su GitHub: {e}")


def main():
    """Funzione principale."""
    try:
        creds = get_credentials()
    except ValueError as e:
        logging.error(e)
        return
    except ImportError as e:
         logging.error(e)
         return

    with requests.Session() as session:
        # 1. Login (ora usa cookie)
        ok = login_escp(session, raw_cookies=creds.get("escp_cookies"))
        if not ok:
            logging.error("Autenticazione ESCP fallita. Controlla i cookie o le credenziali.")
            return

        # 2. Fetch eventi (con API e parametri corretti)
        events = fetch_calendar_events(session)
        if not events:
            logging.warning("Nessun evento estratto dal portale.")
            # Gestiamo il caso di nessun evento creando un file vuoto più avanti

        # 3. Crea file ICS (anche se vuoto, per cancellare eventi vecchi)
        ics_filename = create_ics_file(events) # Può restituire None se events è vuoto
        
        # Gestione del caso in cui non ci sono eventi -> crea file vuoto
        if not ics_filename and not events:
             logging.info("Nessun evento, nessun file ICS creato. Procedo con la creazione di un file vuoto per GitHub.")
             try:
                   ics_filename = "escp_calendar.ics"
                   with open(ics_filename, "w", encoding="utf-8") as f:
                       # Creiamo un calendario ICS valido ma vuoto
                        empty_cal = Calendar()
                        empty_cal.extra.append(ContentLine(name="X-WR-CALNAME", value=CAL_NAME))
                        empty_cal.extra.append(ContentLine(name="PRODID", value="-//ESCP Sync Script//ics.tools//EN"))
                        f.write(empty_cal.serialize())
                   logging.info(f"Creato file ICS vuoto '{ics_filename}' per l'aggiornamento.")
             except IOError as e:
                   logging.error(f"Impossibile creare il file ICS vuoto '{ics_filename}': {e}")
                   return # Non possiamo procedere senza un file
        elif not ics_filename and events:
             # C'erano eventi ma la creazione è fallita per un altro motivo (es IO Error)
             logging.error("Fallita la creazione del file ICS nonostante ci fossero eventi.")
             return


        # 4. Upload su repo privato
        if creds.get("private_repo_name"):
            upload_to_github(
                token=creds["github_token"],
                repo_name=creds["private_repo_name"],
                file_path=ics_filename,
                commit_message="Update ESCP Calendar" # Messaggio più generico
            )
        else:
             logging.warning("Nome repository privato non configurato, caricamento saltato.")


        # 5. (Opzionale) Mirror pubblico
        if creds.get("public_repo_name"):
            logging.info("Copia nel repo pubblico…")
            upload_to_github(
                token=creds["github_token"],
                repo_name=creds["public_repo_name"],
                file_path=ics_filename,
                commit_message="Update ESCP Public Calendar"
            )
            owner_repo = creds['public_repo_name']
            public_url = f"https://raw.githubusercontent.com/{owner_repo}/main/{os.path.basename(ics_filename)}"
            logging.info(f"URL pubblico da usare (es. in Google Calendar): {public_url}")
        else:
             logging.info("Repository pubblico non configurato, copia saltata.")

    logging.info("Script completato.")

# --- Esecuzione ---
if __name__ == "__main__":
    main()
