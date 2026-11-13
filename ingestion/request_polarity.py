"""Bounded source-refusal evidence for optional model extraction review.

This is not a general negation/NLU system. It recognizes explicit refusal or
cancellation of check-in, checkout, arrival-delay, cleaning and parking services
in six languages. Its result may add a review flag; it never rewrites intent,
grants permission, or establishes an affirmative request. Uncovered wording must
not be interpreted as proof of positive intent. No I/O or parser side effects.
"""
from __future__ import annotations

import re
import unicodedata

__all__ = ['declined_intents']

_QUOTES = str.maketrans({'’':"'",'‘':"'",'ʼ':"'",'＇':"'",'‐':'-','‑':'-','–':'-','—':'-'})
_JOINERS = {'en':'but|and|however','de':'aber|und|sondern','it':'ma|e|pero',
            'fr':'mais|et','es':'pero|y|sino','nl':'maar|en'}
_REFUSALS = {
    'en':r"\b(?:(?:do not|don't|does not|doesn't|would not|wouldn't)\s+(?:want|need|request|require|like)|no longer\s+(?:want|need|request|require))\b",
    'de':r'\b(?:mocht\w*|will|wollen|brauch\w*|benotig\w*)\s+(?:(?:heute|morgen|jetzt|bitte)\s+){0,2}(?:kein\w*|nicht(?:\s+mehr)?)\b',
    'it':r'\bnon\s+(?:voglio|vogliamo|desidero|desideriamo|vorrei|richiedo|richiediamo|ho bisogno|abbiamo bisogno)\b',
    'fr':r"\b(?:ne\s+(?:veux|voulons|voudrais|voudrions|voudrons|souhaite|souhaitons|demande|demandons)\s+(?:pas|plus)|n'\s*(?:ai|avons)\s+(?:pas|plus)\s+besoin)\b",
    'es':r'\bno\s+(?:quiero|queremos|necesito|necesitamos|deseo|deseamos|solicito|solicitamos)\b',
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
_SERVICES = {
    'early_checkin':r'\b(?:check[ -]?in|incheck\w*|eincheck\w*)\b',
    'late_checkout':r'\b(?:check[ -]?out|uitcheck\w*|auscheck\w*)\b',
    'arrival_delay':r'\b(?:arrival\s+delay|late\s+arrival|delayed\s+arrival|verspat\w*\s+anreise|arrivo\s+in\s+ritardo|arrivee\s+tardive|llegada\s+tardia|late\s+aankomst)\b',
    'cleaning_request':r'\b(?:cleaning|housekeeping|reinigung|zimmerreinigung|pulizia|pulizie|menage|nettoyage|limpieza|schoonmaak)\b',
    'parking':r'\b(?:parking|parkplatz\w*|parcheggio|aparcamiento|estacionamiento|parkeer\w*)\b',
}
# A bare no/kein/geen is accepted only immediately before a service object.
_BARE = {'en':r'\bno\s+','de':r'\bkein\w*\s+','it':r'\b(?:niente|nessun\w*)\s+',
         'fr':r'\bpas\s+de\s+','es':r'\bsin\s+','nl':r'\bgeen\s+'}


def _fold(text):
    text=unicodedata.normalize('NFKD',text.translate(_QUOTES).casefold())
    return ''.join(char for char in text if not unicodedata.combining(char))


def declined_intents(text: str, language: str) -> set[str]:
    """Return supported service labels with explicit source refusal evidence."""
    if not isinstance(text,str) or not isinstance(language,str):
        return set()
    language=language.casefold().split('-')[0].split('_')[0]
    if language not in _REFUSALS:
        return set()
    declined=set()
    boundary=r'[.!?;,\n]|\b(?:'+_JOINERS[language]+r')\b'
    for clause in re.split(boundary,_fold(text)):
        refusals=list(re.finditer(_REFUSALS[language],clause))
        objects=[]
        for refusal in refusals:
            tail=clause[refusal.end():].strip()
            # Negating cancellation means retaining the service, not declining.
            if re.match(_WAIT,tail) or re.match(r'^(?:(?:to|a|di|de|te|zu)\s+)*'+_CANCEL[language],tail):
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
                if (re.match if direct else re.search)(pattern,tail):
                    declined.add(intent)
    return declined
