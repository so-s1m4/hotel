"""Bounded source-refusal evidence for optional model extraction review.

This is not a general negation/NLU system. It recognizes explicit refusal or
cancellation of check-in, checkout, arrival-delay, cleaning, parking and luggage
storage in six languages. Its result may add a review flag; it never rewrites intent,
grants permission, or establishes an affirmative request. Uncovered wording must
not be interpreted as proof of positive intent. No I/O or parser side effects.
Refusal remains conservative for all items of the same service label: the event
contract does not represent an independent room or area target for each item.
"""
from __future__ import annotations

import re
import unicodedata

__all__ = ['declined_intents']

_QUOTES = str.maketrans({'’':"'",'‘':"'",'ʼ':"'",'＇':"'",'‐':'-','‑':'-','–':'-','—':'-'})
_JOINERS = {'en':'but|and|however','de':'aber|und|sondern','it':'ma|e|pero',
            'fr':'mais|et','es':'pero|y|sino','nl':'maar|en'}
_ADDITIVE = {'en':'and','de':'und','it':'e','fr':'et','es':'y','nl':'en'}
_OBJECT_DETERMINERS = {
    'en':r'a|an|the|any|my|our',
    'de':r'der|die|das|den|dem|ein|eine|einen|einem|mein|meine|meinen|unser|unsere|unseren',
    'it':r'il|lo|la|i|gli|le|un|uno|una|nostro|nostra|nostri|nostre',
    'fr':r'le|la|les|un|une|de|du|des|notre|nos',
    'es':r'el|la|los|las|un|una|unos|unas|mi|mis|nuestro|nuestra|nuestros|nuestras',
    'nl':r'de|het|een|mijn|onze|ons',
}
_REFUSALS = {
    'en':(r"\b(?:(?:do not|don't|does not|doesn't|would not|wouldn't)\s+(?:want|need|request|require|like)|no longer\s+(?:want|need|request|require))\b"
          r"|\b(?:am|are|is|was|were)\s+not\s+(?:asking|requesting|intending|planning)\b"
          r"|\b(?:refuse|decline)\b"
          r"|^\s*(?:please\s+)?(?:do\s+not|don't)\b"),
    'de':r'\b(?:mocht\w*|will|wollen|brauch\w*|benotig\w*)\s+(?:(?:heute|morgen|jetzt|bitte)\s+){0,2}(?:kein\w*|nicht(?:\s+mehr)?)\b',
    'it':r'\bnon\s+(?:voglio|vogliamo|desidero|desideriamo|vorrei|richiedo|richiediamo|ho bisogno|abbiamo bisogno)\b',
    'fr':r"\b(?:ne\s+(?:veux|voulons|voudrais|voudrions|voudrons|souhaite|souhaitons|demande|demandons)\s+(?:pas|plus)|n'\s*(?:ai|avons)\s+(?:pas|plus)\s+besoin)\b",
    'es':(r'\bno\s+(?:quiero|queremos|necesito|necesitamos|deseo|deseamos|solicito|solicitamos)\b'
          r'|^\s*(?:por\s+favor\s+)?no(?=\s+(?:(?:me|nos)\s+)?(?:limpiar|limpie|limpien|limpies|limpieis)\b)'),
    'nl':r'\b(?:wil|willen)\s+(?:geen|niet(?:\s+meer)?)\b',
}
_CANCEL = {
    'en':r'\b(?:cancel|withdraw|revoke)\b',
    'de':r'\b(?:stornier\w*|absagen|widerruf\w*)\b',
    'it':r'\b(?:annull\w*|cancell\w*)\b',
    'fr':r'\b(?:annul\w*)\b',
    'es':r'\b(?:cancel\w*|anul\w*)\b',
    'nl':r'\b(?:annul\w*)\b',
}
# A refusal to wait for a service is not a refusal of that service.
_WAIT = r'^(?:(?:to|a|di|de|te|zu|lange|long|longer|lang|piu|mas|plus)\s+)*(?:wait\w*|warten|attendre|aspettare|esperar|wachten)\b'
_COURTESY = r'^(?:problem\w*|problema\w*|probleme\w*|worries|probleem\w*)\b'
_EN_NON_SERVICE_TARGET = r'^(?:to\s+)?worry\b'
_EN_POSITIVE_REMINDER = (
    r'^(?:to\s+)?forget\s+to\s+(?:clean|check\s+(?:(?:me|us)\s+)?(?:in|out)'
    r'|store|keep|hold|reserve|book|arrange|request|provide)\b'
)
_EN_NEGATED_CANCEL_REMINDER = r'^(?:to\s+)?forget\s+(?:not\s+to|to\s+not)\s+'+_CANCEL['en']
_SERVICES = {
    'early_checkin':r'\b(?:check[ -]?in|check\s+(?:me|us)\s+in|incheck\w*|eincheck\w*)\b',
    'late_checkout':r'\b(?:check[ -]?out|check\s+(?:me|us)\s+out|uitcheck\w*|auscheck\w*|depart\s+tardif|salida\s+tardia)\b',
    'arrival_delay':r'\b(?:arrival\s+delay|late\s+arrival|delayed\s+arrival|verspat\w*\s+anreise|arrivo\s+in\s+ritardo|arrivee\s+tardive|llegada\s+tardia|late\s+aankomst)\b',
    'cleaning_request':r'\b(?:clean|cleaning|housekeeping|reinigung|zimmerreinigung|pulizia|pulizie|menage|nettoyage|limpieza|limpiar|limpie|limpien|limpies|limpieis|schoonmaak)\b',
    'parking':r'\b(?:parking|parkplatz\w*|parcheggio|aparcamiento|estacionamiento|parkeer\w*)\b',
    'luggage_storage':(
        r'\b(?:(?:luggage|baggage|bag)\s+storage|left[ -]luggage'
        r'|(?:store|keep|hold)\s+(?:(?:my|our|the|these|those)\s+)?(?:bags|baggage|luggage|suitcases)'
        r'|gepackaufbewahrung|kofferaufbewahrung'
        r'|(?:deposito|custodia)\s+(?:(?:di|dei)\s+)?bagagli'
        r'|consigne\s+(?:a\s+)?bagages|garde[ -]bagages|depot\s+de\s+bagages'
        r'|(?:guardar|guarde|guarden)\s+(?:(?:el|la|los|las|mi|mis|nuestro|nuestra|nuestros|nuestras)\s+)?(?:equipaje|maletas?|bolsos?)'
        r'|consigna\s+de\s+equipaje|deposito\s+de\s+equipaje'
        r'|bagageopslag|bagagedepot|bagagebewaring)\b'
    ),
}
# A bare no/kein/geen is accepted only immediately before a service object.
_BARE = {'en':r'\bno\s+','de':r'\bkein\w*\s+','it':r'\b(?:niente|nessun\w*)\s+',
         'fr':r'\bpas\s+de\s+','es':r'\bsin\s+','nl':r'\bgeen\s+'}
# Only a complete, bounded nominal service phrase can inherit a refusal across
# an additive conjunction. Service verbs are deliberately excluded: "and hold
# our bags" is an independent imperative. Predicates such as "parking is
# essential" and unrecognized/free-form continuations retain the old split
# behavior. This is not a general analysis of coordinated noun phrases.
_NOMINAL_OBJECT = (
    r'(?:check-?in|check-?out|depart\s+tardif|salida\s+tardia'
    r'|arrival\s+delay|late\s+arrival|delayed\s+arrival|verspat\w*\s+anreise'
    r'|arrivo\s+in\s+ritardo|arrivee\s+tardive|llegada\s+tardia|late\s+aankomst'
    r'|cleaning|housekeeping|reinigung|zimmerreinigung|pulizia|pulizie|menage|nettoyage|limpieza|schoonmaak'
    r'|parking(?:\s+spaces?)?|parkplatz\w*|parcheggio|aparcamiento|estacionamiento'
    r'|parkeerplaats(?:en)?|parkeergelegenheid|parkeerplek(?:ken)?|parkeerruimte'
    r'|(?:luggage|baggage|bag)\s+storage|left[ -]luggage|gepackaufbewahrung|kofferaufbewahrung'
    r'|(?:deposito|custodia)\s+(?:(?:di|dei)\s+)?bagagli'
    r'|consigne\s+(?:a\s+)?bagages|garde[ -]bagages|depot\s+de\s+bagages'
    r'|consigna\s+de\s+equipaje|deposito\s+de\s+equipaje'
    r'|bagageopslag|bagagedepot|bagagebewaring)'
)


def _fold(text):
    text=unicodedata.normalize('NFKD',text.translate(_QUOTES).casefold())
    return ''.join(char for char in text if not unicodedata.combining(char))


def _clauses(text, language):
    for sentence in re.split(r'[.!?;,\n]', text):
        parts=re.split(r'\b('+_JOINERS[language]+r')\b', sentence)
        clause=parts[0]
        for index in range(1,len(parts),2):
            joiner,tail=parts[index:index+2]
            object_text=re.sub(r'^(?:(?:'+_OBJECT_DETERMINERS[language]+r')\s+)+','',tail.strip())
            if joiner != _ADDITIVE[language] or not re.fullmatch(_NOMINAL_OBJECT,object_text):
                yield clause
                clause=tail
            else:
                clause+=joiner+tail
        yield clause


def declined_intents(text: str, language: str) -> set[str]:
    """Return supported service labels with explicit source refusal evidence."""
    if not isinstance(text,str) or not isinstance(language,str):
        return set()
    language=language.casefold().split('-')[0].split('_')[0]
    if language not in _REFUSALS:
        return set()
    declined=set()
    for clause in _clauses(_fold(text),language):
        refusals=list(re.finditer(_REFUSALS[language],clause))
        objects=[]
        for refusal in refusals:
            if language == 'en' and refusal.group() in {'refuse','decline'} and re.search(
                    r"\b(?:not|don't|never)\s*$",clause[:refusal.start()]):
                continue
            tail=clause[refusal.end():].strip()
            # Negating cancellation means retaining the service, not declining.
            if re.match(_WAIT,tail) or re.match(r'^(?:(?:to|a|di|de|te|zu)\s+)*'+_CANCEL[language],tail):
                continue
            # A reassurance or a clear positive service reminder is not a
            # refusal. Keep cancellations and negated service actions reviewed.
            # Only the two immediate negated-cancel reminders below are resolved;
            # other nested constructions remain conservative and unsupported.
            if language == 'en' and re.match(_EN_NON_SERVICE_TARGET,tail):
                continue
            if language == 'en' and re.match(_EN_NEGATED_CANCEL_REMINDER,tail):
                continue
            if (language == 'en' and re.match(_EN_POSITIVE_REMINDER,tail)
                    and not re.search(r"\b(?:not|no|never|without|don't)\b|"+_CANCEL['en'],tail)):
                continue
            objects.append((tail,False))
        for cancellation in re.finditer(_CANCEL[language],clause):
            prefix=clause[:cancellation.start()]
            if refusals or re.search(r"\b(?:not|don't|never|nicht|non|pas|no|niet)\s*$",prefix):
                continue
            objects.append((clause.strip(),False))
        for bare in re.finditer(_BARE[language],clause):
            tail=clause[bare.end():].strip()
            if not re.match(_COURTESY,tail):
                objects.append((tail,True))
        for tail,direct in objects:
            for intent,pattern in _SERVICES.items():
                matches=[re.match(pattern,tail)] if direct else re.finditer(pattern,tail)
                # "No parking problem" describes a problem, not a declined
                # parking service. Keep the existing leading courtesy guard.
                if any(match and not re.match(_COURTESY,tail[match.end():].lstrip())
                       for match in matches):
                    declined.add(intent)
    return declined
