"""Rechtstexte als eigenständige Module (Impressum & Datenschutz)."""


def render_impressum_module(back_link: str = "/") -> str:
    """Vollständiger Impressum-Block als eigenständiges Modul."""
    return f""" 
<h1>Impressum</h1>
<p><a class="button" href="{back_link}">Zurück</a></p>

<div class="box">
  <h3>Anbieterkennzeichnung gemäß § 5 DDG</h3>
  <p>
    <strong>Foad Amini</strong><br>
    BIMPruef Platform<br>
    E-Mail: <a href="mailto:amini.foad@gmail.com">amini.foad@gmail.com</a>
  </p>
</div>

<div class="box">
  <h3>Verantwortlich für journalistisch-redaktionelle Inhalte (§ 18 Abs. 2 MStV)</h3>
  <p>Foad Amini</p>
</div>

<div class="box">
  <h3>Kontakt</h3>
  <p>
    Bei Fragen zu dieser Website oder den bereitgestellten Diensten kontaktieren Sie uns bitte per
    E-Mail unter <a href="mailto:amini.foad@gmail.com">amini.foad@gmail.com</a>.
  </p>
</div>

<div class="box">
  <h3>Haftung für Inhalte</h3>
  <p>
    Die Inhalte dieser Website wurden mit größtmöglicher Sorgfalt erstellt. Für die Richtigkeit,
    Vollständigkeit und Aktualität der Inhalte kann jedoch keine Gewähr übernommen werden.
  </p>
</div>

<div class="box">
  <h3>Haftung für Links</h3>
  <p>
    Diese Website kann Links zu externen Websites Dritter enthalten. Auf deren Inhalte haben wir
    keinen Einfluss. Für die Inhalte der verlinkten Seiten ist stets der jeweilige Anbieter oder
    Betreiber verantwortlich.
  </p>
</div>

<div class="box">
  <h3>Urheberrecht</h3>
  <p>
    Die durch den Seitenbetreiber erstellten Inhalte und Werke auf diesen Seiten unterliegen dem
    deutschen Urheberrecht. Vervielfältigung, Bearbeitung, Verbreitung und jede Art der Verwertung
    außerhalb der Grenzen des Urheberrechtes bedürfen der schriftlichen Zustimmung des jeweiligen
    Autors bzw. Erstellers.
  </p>
</div>

<p class="small">Stand: 22. April 2026</p>
"""


def render_datenschutz_module(back_link: str = "/") -> str:
    """Vollständiger Datenschutz-Block als eigenständiges Modul."""
    return f"""
<h1>Datenschutzerklärung</h1>
<p><a class="button" href="{back_link}">Zurück</a></p>

<div class="box">
  <h3>1. Verantwortlicher</h3>
  <p>
    <strong>Foad Amini</strong><br>
    E-Mail: <a href="mailto:amini.foad@gmail.com">amini.foad@gmail.com</a>
  </p>
</div>

<div class="box">
  <h3>2. Zwecke und Rechtsgrundlagen der Verarbeitung</h3>
  <p>
    Wir verarbeiten personenbezogene Daten, soweit dies für den Betrieb der Plattform,
    die Bereitstellung der Upload- und Analysefunktionen sowie zur IT-Sicherheit erforderlich ist.
  </p>
  <ul>
    <li><strong>Bereitstellung des Dienstes</strong> (Art. 6 Abs. 1 lit. b DSGVO)</li>
    <li><strong>Betriebssicherheit, Missbrauchserkennung und Fehleranalyse</strong> (Art. 6 Abs. 1 lit. f DSGVO)</li>
    <li><strong>Erfüllung gesetzlicher Pflichten</strong> (Art. 6 Abs. 1 lit. c DSGVO), soweit anwendbar</li>
  </ul>
</div>

<div class="box">
  <h3>3. Verarbeitete Datenkategorien</h3>
  <ul>
    <li><strong>Nutzungsdaten:</strong> Datum/Uhrzeit, aufgerufene Endpunkte, technische Metadaten</li>
    <li><strong>Upload-Daten:</strong> hochgeladene IFC-Dateien (.ifc, .ifczip), Dateinamen</li>
    <li><strong>Kommunikationsdaten:</strong> Inhalte von Kontaktanfragen per E-Mail</li>
  </ul>
</div>

<div class="box">
  <h3>4. Speicherdauer</h3>
  <p>
    Hochgeladene IFC-Dateien und zugehörige Session-Daten werden grundsätzlich nur temporär
    gespeichert und spätestens nach 24 Stunden automatisiert gelöscht.
    Länger gespeicherte Daten entstehen nur, wenn dies technisch notwendig oder rechtlich
    vorgeschrieben ist.
  </p>
</div>

<div class="box">
  <h3>5. Empfänger und Auftragsverarbeiter</h3>
  <p>
    Ein Zugriff auf Daten erfolgt nur, soweit dies für den technischen Betrieb erforderlich ist.
    Sofern externe Hosting- oder Infrastruktur-Dienstleister eingesetzt werden, erfolgt dies auf
    Grundlage eines Auftragsverarbeitungsvertrages nach Art. 28 DSGVO.
  </p>
</div>

<div class="box">
  <h3>6. Drittlandübermittlungen</h3>
  <p>
    Sofern Daten außerhalb der EU/des EWR verarbeitet werden, erfolgt dies nur unter Einhaltung
    der Voraussetzungen der Art. 44 ff. DSGVO (z. B. Angemessenheitsbeschluss oder geeignete
    Garantien wie EU-Standardvertragsklauseln).
  </p>
</div>

<div class="box">
  <h3>7. Ihre Rechte</h3>
  <ul>
    <li>Auskunft (Art. 15 DSGVO)</li>
    <li>Berichtigung (Art. 16 DSGVO)</li>
    <li>Löschung (Art. 17 DSGVO)</li>
    <li>Einschränkung der Verarbeitung (Art. 18 DSGVO)</li>
    <li>Datenübertragbarkeit (Art. 20 DSGVO)</li>
    <li>Widerspruch gegen bestimmte Verarbeitungen (Art. 21 DSGVO)</li>
    <li>Widerruf erteilter Einwilligungen (Art. 7 Abs. 3 DSGVO)</li>
    <li>Beschwerde bei einer Datenschutzaufsichtsbehörde (Art. 77 DSGVO)</li>
  </ul>
</div>

<div class="box">
  <h3>8. Bereitstellungspflicht</h3>
  <p>
    Die Bereitstellung bestimmter Daten ist für die Nutzung der Plattform technisch erforderlich.
    Ohne diese Daten können zentrale Funktionen (z. B. Datei-Upload und Modellvergleich) nicht
    bereitgestellt werden.
  </p>
</div>

<div class="box">
  <h3>9. Automatisierte Entscheidungsfindung</h3>
  <p>
    Eine automatisierte Entscheidungsfindung einschließlich Profiling im Sinne von Art. 22 DSGVO
    findet nicht statt.
  </p>
</div>

<div class="box">
  <h3>10. Änderungen dieser Datenschutzerklärung</h3>
  <p>
    Wir behalten uns vor, diese Datenschutzerklärung anzupassen, wenn sich rechtliche,
    technische oder organisatorische Rahmenbedingungen ändern.
  </p>
</div>

<p class="small">Stand: 22. April 2026</p>
"""
