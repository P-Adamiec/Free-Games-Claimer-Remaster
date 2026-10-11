"""Help texts in Dutch: how signing in works per store, and the Uitleg page.

One place for the settings page ("Hoe inloggen werkt" per store) and the Uitleg
page. Written for someone who is not technical. Based on what the bot's code does
(src/stores/*.py, src/core/claimer.py) and on the stores' own help pages.
"""

from __future__ import annotations

MODES = {
    "auto": {"label": "Automatisch", "tone": "ok",
             "text": "na de eerste keer inloggen doet de bot alles zelf, ook de inlogcode als je de "
                     "authenticator-sleutel invult."},
    "approve": {"label": "Af en toe zelf", "tone": "busy",
                "text": "de bot logt zelf in, maar soms moet je iets goedkeuren of een code of captcha "
                        "invullen via het scherm van de bot."},
    "manual": {"label": "Altijd zelf", "tone": "bad",
               "text": "inloggen doe je zelf via het scherm van de bot; daarna onthoudt de bot je een tijd."},
}

KEY_TRADEOFF = ("Afweging: met de sleutel maakt de bot zelf inlogcodes. Je wachtwoord en je tweede stap "
                "staan dan samen op de server; wie daar bij kan, kan dus ook inloggen.")
KEY_EXISTING = ("Staat tweestapsverificatie al aan met een app? De oude sleutel is dan niet meer te zien. Verwijder "
                "de authenticator-app bij de winkel en stel opnieuw in (nieuwe sleutel; zet de nieuwe QR-code ook "
                "weer in je eigen app), of exporteer de sleutel uit een app die dat kan, zoals Aegis of 2FAS.")
KEY_NO_TEXT = ("Zie je alleen een QR-code en geen sleutel als tekst? Scan de code dan met Aegis of 2FAS: die apps "
               "kunnen de sleutel daarna tonen (bij de details of via exporteren).")

GENERAL = {
    "intro": ("De bot logt voor je in bij de winkels en claimt de gratis games. Meestal gaat dat vanzelf. "
              "Soms wil een winkel zeker weten dat jij het bent: dan moet je één keer iets doen via het "
              "scherm van de bot. Hieronder staat per winkel wat je kunt verwachten."),
    "sections": [
        {"id": "scherm", "title": "Het scherm van de bot",
         "paragraphs": [
             "De bot gebruikt een echte browser (Chrome) op de server. Via \"Scherm van de bot openen\" op het "
             "overzicht kijk je mee en kun je daarin klikken en typen, alsof het je eigen browser is.",
             "Het scherm vraagt eerst om het schermwachtwoord; dat staat op je overzicht onder de knop. "
             "Is het scherm zwart of leeg, dan is de bot op dat moment niet bezig. Dat is normaal.",
         ]},
        {"id": "wacht", "title": "De bot wacht op je",
         "paragraphs": [
             "Krijg je een melding dat de bot op je wacht (bijvoorbeeld \"manual login needed\" of een vraag om "
             "een code)? Dan staat de browser van de bot op een inlogpagina, een code-scherm of een captcha.",
         ],
         "steps": [
             "Open de website en klik op \"Scherm van de bot openen\".",
             "Rond af wat je ziet: wachtwoord, code uit je e-mail of sms, goedkeuren in de app van de winkel, "
             "of de captcha.",
             "Klik verder niets weg: de bot ziet zelf dat het gelukt is en gaat door.",
         ],
         "bullets": [
             "De bot wacht standaard 3 minuten (instelling \"Wachttijd voor handmatig inloggen\" bij Overig).",
             "Ben je te laat, dan slaat de bot die winkel over tot de volgende run. Er gaat niets kapot.",
             "Een winkel die geen antwoord kreeg, vraagt in dezelfde run niet nog een keer.",
         ]},
        {"id": "onthouden", "title": "Waarom je niet steeds opnieuw hoeft in te loggen",
         "paragraphs": [
             "Elke winkel heeft een eigen browserprofiel op de server. Daarin blijven je inlog-cookies "
             "bewaard, net als in je eigen browser. Waar de winkel het aanbiedt, vinkt de bot \"onthoud mij\" "
             "of \"vertrouw deze browser\" aan.",
             "Opnieuw inloggen is nodig als de winkel je sessie laat verlopen, als je je wachtwoord wijzigt, "
             "als je \"overal uitloggen\" gebruikt, of als de winkel een nieuwe beveiligingscontrole doet. "
             "Met wachtwoord (en eventueel sleutel) ingevuld doet de bot dat meestal zelf.",
         ]},
        {"id": "eerste-keer", "title": "Tip: de eerste keer",
         "paragraphs": [
             "Vul je gegevens in bij Instellingen, druk daarna op \"Nu uitvoeren\" op het overzicht en open "
             "meteen het scherm van de bot. Houd je telefoon bij de hand voor codes en goedkeuringen. Zo doe "
             "je alle eerste logins in één keer, in plaats van om 06:00 of 18:00 als je slaapt.",
             "Zet ook meldingen aan (Instellingen > Meldingen): via Discord of Apprise (bijvoorbeeld de app "
             "ntfy op je telefoon) hoor je het meteen als de bot op een goedkeuring wacht.",
         ]},
        {"id": "sleutel", "title": "De authenticator-sleutel",
         "paragraphs": [
             "Bij een aantal winkels kun je een \"authenticator-sleutel\" invullen. Dat is de geheime code achter "
             "de QR-code die je met een app zoals Google Authenticator, Microsoft Authenticator, Aegis of 2FAS "
             "scant. Met die sleutel maakt de bot zelf de 6-cijferige inlogcode, en hoef je niets goed te keuren.",
             "Je vindt de sleutel bij het instellen van de authenticator-app: onder de QR-code staat een link "
             "zoals \"Kun je de code niet scannen?\" of \"Enter key manually\". Kopieer de lange code van "
             "letters en cijfers en scan daarna ook de QR-code met je eigen app, zodat je zelf ook kunt inloggen.",
             KEY_EXISTING,
             KEY_TRADEOFF,
         ]},
    ],
}


def _h(mode, summary, bot=(), you=(), twofa=(), key_steps=(), notes=()) -> dict:
    return {"mode": mode, "summary": summary, "bot": list(bot), "you": list(you), "twofa": list(twofa),
            "key_steps": list(key_steps), "notes": list(notes)}


FIRST_LOGIN_VNC = ("Geen e-mailadres of wachtwoord ingevuld? Dan wacht de bot tot je zelf inlogt via het "
                   "scherm van de bot.")

STORE_HELP: dict[str, dict] = {
    "steam": _h(
        "approve",
        "De bot vult je gebruikersnaam en wachtwoord in; Steam Guard keur je zelf goed op je telefoon.",
        bot=["Vult je Steam-gebruikersnaam (niet je e-mailadres) en wachtwoord in.",
             "Laat \"Onthoud mij\" aangevinkt, zodat Steam je lang ingelogd houdt.",
             "Activeert Steam-sleutels die Fanatical oplevert, als Steam en Fanatical allebei aan staan."],
        you=["De eerste keer, en als Steam later opnieuw om bevestiging vraagt: keur de aanmelding goed in de "
             "Steam-app op je telefoon, of typ de Steam Guard-code (uit de app of je e-mail) over in het scherm "
             "van de bot.",
             "Een captcha bij het inloggen los je ook zelf op via het scherm.",
             FIRST_LOGIN_VNC],
        twofa=["Steam Guard werkt alleen met de eigen Steam-app (goedkeuren of een code van 5 tekens) of met "
               "codes per e-mail. Een gewone authenticator-sleutel bestaat bij Steam niet, dus dit kan de bot "
               "nooit zelf. Instellen doe je in Steam bij Accountgegevens > \"Steam Guard beheren\".",
               "Een e-mailcode van Steam kan even op zich laten wachten; de bot wacht standaard 3 minuten."],
        notes=["De bot claimt alleen games die je mag houden (\"free to keep\"), geen free-to-play of demo's.",
               "DLC lukt alleen als je het basisspel hebt of als dat basisspel ook gratis is."]),
    "epic": _h(
        "approve",
        "Inloggen gaat vanzelf, met de sleutel ook de inlogcode. Af en toe vraagt Epic een captcha.",
        bot=["Vult e-mail en wachtwoord in en vinkt \"Onthoud mij\" aan, en op het code-scherm \"dit apparaat "
             "onthouden\".",
             "Vult de 6-cijferige code zelf in als je de authenticator-sleutel invult; lukt dat niet, dan een "
             "herstelcode als je die invult. Gebruikte herstelcodes onthoudt de bot.",
             "Claimt ook de wekelijkse gratis mobiele game, als dat aan staat (zelfde login)."],
        you=["Captchas (\"ben je een mens\"-puzzels) los je zelf op via het scherm, ook als die pas bij het "
             "claimen verschijnt. Dat gebeurt vooral bij een nieuw browserprofiel.",
             "Zonder sleutel: typ de code uit je e-mail of sms over in het scherm van de bot.",
             "Gebruik je de Epic Authenticator (goedkeuren met een nummer in de Epic Games-app)? Dat kan de bot "
             "niet; maak dan de authenticator-app je hoofdmethode, of keur zelf goed via het scherm.",
             FIRST_LOGIN_VNC],
        twofa=["Automatisch: authenticator-app (sleutel) en herstelcodes (\"Try another way\").",
               "Zelf via het scherm: code per e-mail of sms, goedkeuren in de Epic Games-app."],
        key_steps=["Ga op epicgames.com naar Account > \"Wachtwoord en beveiliging\" (Password and security).",
                   "Kies bij tweestapsverificatie \"Authenticator-app\" > \"Instellen\" (Set up).",
                   "Kopieer de sleutel die bij de QR-code staat naar het veld hieronder en scan de QR-code ook met "
                   "je eigen app. " + KEY_NO_TEXT,
                   "Rond af met de code uit je app. Bewaar de herstelcodes (\"Get Backup Codes\"); die kun je bij "
                   "\"Meer opties\" invullen.",
                   KEY_EXISTING, KEY_TRADEOFF],
        notes=["Sommige games zijn in jouw land niet beschikbaar; die slaat de bot over.",
               "Een ouderlijk-toezicht-pincode kun je bij \"Meer opties\" invullen."]),
    "fab": _h(
        "auto",
        "Gebruikt je Epic-account; meestal ben je al ingelogd via Epic.",
        bot=["Gebruikt dezelfde browser als Epic, dus meestal is er geen aparte login.",
             "Is dat toch nodig, dan logt de bot in met je Epic-gegevens en de Epic-sleutel (als ingevuld).",
             "Accepteert de licentie en de afstandsverklaring alleen als je dat hieronder aan laat staan."],
        you=["Alleen als Epic een code of captcha vraagt: rond dat af via het scherm van de bot."],
        twofa=["De Epic-authenticator-sleutel werkt ook voor Fab. Herstelcodes gebruikt de bot hier niet."],
        notes=["Fab staat standaard uit; zet het aan als je de gratis Unreal-assets wilt."]),
    "prime": _h(
        "auto",
        "Met wachtwoord en sleutel helemaal automatisch; zonder sleutel keur je de aanmelding zelf goed.",
        bot=["Vult e-mail en wachtwoord van je Amazon-account in.",
             "Slaat \"inloggen met een toegangssleutel (passkey)\" over en kiest het wachtwoord.",
             "Vult de code zelf in als je de authenticator-sleutel invult, en vinkt dan aan dat Amazon in "
             "deze browser niet meer om een code vraagt.",
             "Bewaart codes voor GOG en Microsoft, zodat die bots ze kunnen inwisselen."],
        you=["Zonder sleutel: keur de aanmelding goed in de Amazon-app, of typ de sms- of e-mailcode over in "
             "het scherm van de bot.",
             FIRST_LOGIN_VNC],
        twofa=["Automatisch: authenticator-app (sleutel).",
               "Zelf: sms, telefoon, e-mail of goedkeuren in de Amazon-app. Amazon geeft geen herstelcodes."],
        key_steps=["Ga op amazon.nl naar Account en lijsten > Je account > \"Inloggen en beveiliging\" > "
                   "\"Verificatie in twee stappen\".",
                   "Kies bij Authenticator-app \"Nieuwe app toevoegen\". Je bestaande app blijft gewoon werken; "
                   "Amazon staat meerdere apps toe.",
                   "Klik onder de QR-code op de link \"Kunt u de barcode niet scannen?\" (Engels: \"Can't scan the "
                   "barcode?\"): daar staat de sleutel.",
                   "Kopieer die naar het veld hieronder en bevestig bij Amazon met een code (die kun je in je eigen "
                   "app maken door dezelfde QR-code te scannen).",
                   "Amazon vraagt ook een reserve-telefoonnummer; zonder dat kun je verificatie in twee stappen "
                   "niet aanzetten.",
                   KEY_TRADEOFF],
        notes=["Je hebt Amazon Prime nodig om games via Prime Gaming te claimen.",
               "Games die een gekoppeld account vragen (bijvoorbeeld Epic of een uitgever), meldt de bot."]),
    "gog": _h(
        "auto",
        "Met sleutel of herstelcodes automatisch; anders typ je de e-mailcode zelf over.",
        bot=["Vult e-mail en wachtwoord in.",
             "Vult de code in met de authenticator-sleutel, of met een herstelcode (gebruikte codes onthoudt de "
             "bot).",
             "Wisselt GOG-codes van Prime Gaming in.",
             "Opent na een claim de nieuwsbriefinstellingen, tenzij je de nieuwsbrief wilt houden."],
        you=["Zonder sleutel: GOG stuurt een code per e-mail (afzender no-reply@gog.com); typ die over in het "
             "scherm van de bot. GOG vraagt dat bij elke nieuwe browser.",
             "Blijft het inloggen hangen (vaak een captcha), dan vraagt de bot je het af te ronden.",
             FIRST_LOGIN_VNC],
        twofa=["GOG kent e-mailcodes óf een authenticator-app, niet allebei tegelijk.",
               "Automatisch: authenticator-app (sleutel) en herstelcodes. Zelf: e-mailcode."],
        key_steps=["Log in op gog.com en ga naar \"Orders & settings\" / Settings > \"Login and security\" "
                   "(Security).",
                   "Zet bij \"Two-step authentication\" eerst \"Email\" uit en daarna \"Authenticator app\" aan.",
                   "Kopieer de sleutel die bij de QR-code staat naar het veld hieronder en scan de QR-code ook met "
                   "je eigen app. " + KEY_NO_TEXT,
                   "Toont GOG herstelcodes, vul die dan in bij \"Meer opties\".",
                   KEY_TRADEOFF]),
    "microsoft": _h(
        "auto",
        "Met wachtwoord en sleutel automatisch; anders keur je zelf goed of typ je de e-mailcode over.",
        bot=["Vult e-mail en wachtwoord in en kiest het wachtwoord als Microsoft een toegangssleutel "
             "(passkey) voorstelt.",
             "Kiest \"een verificatiecode gebruiken\" in plaats van goedkeuren in de app, en vult de code in "
             "als je de sleutel hebt ingevuld.",
             "Antwoordt \"Ja\" op \"Aangemeld blijven?\" en vinkt \"niet meer vragen op dit apparaat\" aan.",
             "Wisselt Microsoft-/Xbox-codes van Prime Gaming in."],
        you=["Zonder sleutel: kies \"Op een andere manier aanmelden\" en gebruik de code per e-mail, of keur "
             "goed in de Microsoft Authenticator-app (met het getal dat je op het scherm ziet).",
             "Vraagt Microsoft om een toegangssleutel (passkey), kies dan een andere manier.",
             FIRST_LOGIN_VNC],
        twofa=["Automatisch: elke authenticator-app (sleutel).",
               "Zelf: e-mail- of sms-code, goedkeuren in Microsoft Authenticator, toegangssleutel. De "
               "herstelcode van Microsoft is alleen voor noodgevallen; die gebruikt de bot niet."],
        key_steps=["Ga naar account.microsoft.com > Beveiliging > \"Manage how I sign in\" (beheren hoe je je "
                   "aanmeldt).",
                   "Kies \"Add a new way to sign in or verify\" > \"Use an app\".",
                   "Kies \"Set up a different Authenticator app\" en daarna onder de QR-code \"I can't scan the "
                   "bar code\": daar staat de sleutel.",
                   "Kopieer die naar het veld hieronder, voeg dezelfde QR-code ook toe aan je eigen app en bevestig "
                   "met een code. Je bestaande Microsoft Authenticator blijft werken.",
                   KEY_TRADEOFF],
        notes=["Geeft het aankoopvenster van Microsoft een fout, dan meldt de bot dat je die game zelf moet "
               "claimen."]),
    "ubisoft": _h(
        "auto",
        "Met wachtwoord en sleutel automatisch; anders typ je de code zelf over.",
        bot=["Vult e-mail en wachtwoord in en vinkt \"ingelogd blijven\" aan.",
             "Vult de code in als je de authenticator-sleutel hebt ingevuld."],
        you=["Zonder sleutel: typ de code uit je e-mail, sms of app over in het scherm van de bot.",
             "Captchas of beveiligingscontroles los je zelf op.",
             FIRST_LOGIN_VNC],
        twofa=["Automatisch: authenticator-app (sleutel).",
               "Zelf: code per e-mail of sms. Ubisoft geeft 6 herstelcodes, maar die gebruikt de bot (nog) niet; "
               "bewaar ze zelf."],
        key_steps=["Ga naar account.ubisoft.com > Security > \"2-Step verification\".",
                   "Kies de tegel \"Authenticator app\" > \"Activate\". Ubisoft stuurt een e-mail met een link; "
                   "open die om verder te gaan.",
                   "Kopieer de sleutel die bij de QR-code staat naar het veld hieronder en scan de QR-code ook met "
                   "je eigen app. " + KEY_NO_TEXT,
                   KEY_EXISTING, KEY_TRADEOFF]),
    "unity": _h(
        "approve",
        "De bot logt in, maar een Unity-code typ je zelf; de eerste claim vraagt eenmalig je adres.",
        bot=["Vult e-mail en wachtwoord van je Unity ID in.",
             "Accepteert de voorwaarden als je dat aan laat staan, en rekent alleen af als het totaal 0,00 is."],
        you=["Vraagt Unity bij het inloggen een code (Unity maakt tweestapsverificatie steeds vaker verplicht), "
             "typ die dan over in het scherm van de bot en vink \"Remember this device for 30 days\" aan.",
             "De eerste claim: vul in het scherm van de bot je naam en adres in bij het afrekenen, en "
             "beantwoord de belastingvraag (\"exempt from paying consumption tax?\") met No. Daarna gaat het "
             "elke week vanzelf.",
             FIRST_LOGIN_VNC],
        twofa=["De bot kan geen Unity-codes zelf invullen (geen sleutel-veld). Unity werkt met een "
               "authenticator-app en herstelcodes; sms wordt uitgefaseerd."],
        notes=["Unity staat standaard uit.",
               "De Asset Store moet in het Engels staan; anders kan de bot het bedrag niet lezen en claimt de bot "
               "niets."]),
    "aliexpress": _h(
        "approve",
        "De bot logt in met e-mail en wachtwoord; een e-mailcode of schuifpuzzel doe je zelf.",
        bot=["Vult e-mail en wachtwoord in. Inloggen via Google, Facebook of Apple kan de bot niet.",
             "Haalt elke dag de munten op via de mobiele site.",
             "Krijg je maar 1 munt (AliExpress herkent een bot), dan wacht de bot en probeert het later opnieuw."],
        you=["Vraagt AliExpress een 6-cijferige code uit je e-mail of een schuifpuzzel, rond dat dan af in het "
             "scherm van de bot.",
             "Herkent de bot de knop niet, dan vraagt de bot je de munten zelf op te halen via het scherm.",
             FIRST_LOGIN_VNC],
        twofa=["De bot kan geen AliExpress-codes zelf invullen."],
        notes=["Lukt het ophalen vaak niet, haal de munten dan in de AliExpress-app."]),
    "itchio": _h(
        "auto",
        "Met sleutel of herstelcodes automatisch.",
        bot=["Vult e-mail en wachtwoord in.",
             "Vult de code in met de authenticator-sleutel of een herstelcode (gebruikte codes onthoudt de bot)."],
        you=["Zonder sleutel of codes: typ de code zelf over in het scherm van de bot.",
             FIRST_LOGIN_VNC],
        twofa=["Itch.io kent alleen een authenticator-app plus herstelcodes; beide kan de bot zelf gebruiken."],
        key_steps=["Log in op itch.io en open je instellingen (Settings) > tabblad \"Security\".",
                   "Zet tweestapsverificatie aan. De QR-code bevat de sleutel. " + KEY_NO_TEXT,
                   "Kopieer de sleutel naar het veld hieronder en bewaar de herstelcodes (8 cijfers); die kun je "
                   "bij \"Meer opties\" invullen.",
                   KEY_TRADEOFF],
        notes=["Itch.io staat standaard uit.",
               "Sommige giveaways zijn alleen een download; die meldt de bot zonder te claimen."]),
    "fanatical": _h(
        "approve",
        "De bot logt in; vaak vraagt Fanatical om een \"ben je een mens\"-controle die je zelf doet.",
        bot=["Vult e-mail en wachtwoord in via het inlogvenster.",
             "Haalt de Steam-sleutel van een giveaway op en activeert die op Steam als Steam ook aan staat."],
        you=["Captchas rond je zelf af via het scherm van de bot.",
             "Heb je tweestapsverificatie aan bij Fanatical, dan typ je die code ook zelf over: de bot kan "
             "Fanatical-codes niet invullen.",
             FIRST_LOGIN_VNC],
        twofa=["Fanatical biedt een authenticator-app (My Account > Account Settings > Login Details), maar de "
               "bot heeft daar geen sleutel-veld voor; elke code typ je zelf."],
        notes=["Fanatical staat standaard uit.",
               "Sommige acties vragen een gekoppeld Steam-account bij Fanatical.",
               "Staat Steam uit, dan blijft de sleutel in je Fanatical-bibliotheek staan."]),
    "indiegala": _h(
        "manual",
        "Bij het inloggen vink je zelf de captcha aan; daarna ben je 14 dagen ingelogd.",
        bot=["Vult e-mail en wachtwoord in en drukt op inloggen zodra de captcha is aangevinkt.",
             "Waarschuwt in het logboek als de 14 dagen bijna om zijn."],
        you=["Vink de captcha (\"Ik ben geen robot\") aan via het scherm van de bot. Dat is elke 14 dagen "
             "nodig: IndieGala verlengt de sessie niet door gebruik.",
             "Heb je je bij IndieGala aangemeld met Google of Facebook? Stel dan eerst een wachtwoord in "
             "(Library > \"Wallet & Settings\" > \"Account\"); de bot logt in met e-mail en wachtwoord."],
        twofa=["Codes vult de bot bij IndieGala niet zelf in."],
        notes=["IndieGala staat standaard uit."]),
    "alienware": _h(
        "manual",
        "Alleen een melding: de sleutel claim je zelf op de website.",
        bot=["Logt niet in; meldt alleen nieuwe giveaways."],
        you=["Claim de sleutel zelf op alienwarearena.com met \"GET KEY\". Daar heb je soms punten (ARP), een "
             "bepaald niveau, of een code uit de Alienware Arena-app nodig."],
        notes=["Alienware Arena staat standaard uit."]),
}
