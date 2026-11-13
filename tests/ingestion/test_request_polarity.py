"""Specific refusals trigger review evidence; ordinary negative complaints do not."""
import pytest
from ingestion.request_polarity import declined_intents


@pytest.mark.parametrize('language,negative,positive',[
 ('en','I do not want early check-in today at 13:00.','I would like early check-in today at 13:00.'),
 ('de','Ich möchte keinen frühen Check-in heute um 13:00.','Ich möchte einen frühen Check-in heute um 13:00.'),
 ('it','Non voglio il check-in anticipato oggi alle 13:00.','Voglio il check-in anticipato oggi alle 13:00.'),
 ('fr',"Je ne veux pas de check-in anticipé aujourd'hui à 13:00.","Je voudrais un check-in anticipé aujourd'hui à 13:00."),
 ('es','No quiero hacer el check-in temprano hoy a las 13:00.','Quiero hacer el check-in temprano hoy a las 13:00.'),
 ('nl','Ik wil geen vroege check-in vandaag om 13:00.','Ik wil een vroege check-in vandaag om 13:00.'),
])
def test_six_language_refusal_is_specific_and_positive_control_stays_positive(language,negative,positive):
    assert declined_intents(negative,language)=={'early_checkin'}
    assert declined_intents(positive,language)==set()


@pytest.mark.parametrize('language,text',[
 ('en','Please cancel my late checkout.'),('de','Bitte den späten Check-out stornieren.'),
 ('it','Annullate il check-out posticipato.'),('fr','Veuillez annuler le check-out tardif.'),
 ('es','Por favor cancelar el check-out tardío.'),('nl','Annuleer de late checkout.'),
])
def test_explicit_cancellation_targets_only_the_service(language,text):
    assert declined_intents(text,language)=={'late_checkout'}


@pytest.mark.parametrize('language,text',[
 ('en','I do not want to wait for early check-in.'),('de','Ich möchte nicht warten auf den frühen Check-in.'),
 ('it','Non voglio aspettare il check-in anticipato.'),('fr','Je ne veux pas attendre le check-in anticipé.'),
 ('es','No quiero esperar el check-in temprano.'),('nl','Ik wil niet wachten op de vroege check-in.'),
])
def test_disliking_waiting_does_not_decline_the_service(language,text):
    assert declined_intents(text,language)==set()


@pytest.mark.parametrize('language,text',[
 ('en','The wifi does not work; there is no hot water.'),('de','Das WLAN funktioniert nicht.'),
 ('it','Il wifi non funziona.'),('fr','Le wifi ne fonctionne pas.'),('es','El wifi no funciona.'),
 ('nl','De wifi werkt niet.'),('en','No problem with early check-in.'),
 ('en',"I don't want to cancel early check-in."),('fr','Je ne veux pas annuler le check-in.'),
 ('en','Please do not cancel late checkout.'),
])
def test_complaint_courtesy_and_negated_cancellation_are_not_service_refusals(language,text):
    assert declined_intents(text,language)==set()


@pytest.mark.parametrize('separator',['; ','. ',', ',' but ',' and '])
def test_negative_parking_does_not_contaminate_an_independent_positive_service(separator):
    assert declined_intents('I do not need parking'+separator+'I want early check-in today at 13:00.','en')=={'parking'}


@pytest.mark.parametrize('language,text',[
 ('de','Ich brauche keine Reinigung, aber ich möchte einen frühen Check-in.'),
 ('it','Non vogliamo la pulizia e vogliamo il check-in anticipato.'),
 ('fr',"Je n'ai pas besoin de nettoyage et je voudrais un check-in anticipé."),
 ('es','No necesito limpieza y quiero hacer el check-in temprano.'),
 ('nl','Ik wil geen schoonmaak en ik wil een vroege check-in.'),
])
def test_clause_scoping_handles_each_language_conjunction(language,text):
    assert declined_intents(text,language)=={'cleaning_request'}


def test_unicode_quotes_accents_and_regional_language_code_are_folded():
    assert declined_intents('I don’t want early check‑in.','en-GB')=={'early_checkin'}
    assert declined_intents('JE NE VOUDRONS PAS DE CHECK-IN ANTICIPÉ.','fr')=={'early_checkin'}
    assert declined_intents('No parking; can I check in early?','en')=={'parking'}


@pytest.mark.parametrize('text,language',[('', 'en'),(None,'en'),('I do not want early check-in.','xx'),('anything',None)])
def test_unknown_or_unusable_input_makes_no_universal_nlu_claim(text,language):
    assert declined_intents(text,language)==set()


@pytest.mark.parametrize('text,positive,expected',[
 ('I am not asking to check in today at 13:00; please keep my normal arrival tomorrow at 18:00.',
  'I am asking to check in today at 13:00; please keep my normal arrival tomorrow at 18:00.',{'early_checkin'}),
 ('Please do not check us in early tomorrow at 13:00.',
  'Please check us in early tomorrow at 13:00.',{'early_checkin'}),
 ('We refuse early check-in tomorrow at 13:00.',
  'We request early check-in tomorrow at 13:00.',{'early_checkin'}),
 ('Please do not clean our room.', 'Please clean our room.',{'cleaning_request'}),
 ('We decline late checkout.', 'We request late checkout.',{'late_checkout'}),
 ("Please don't check me out late.", 'Please check me out late.',{'late_checkout'}),
])
def test_external_direct_refusal_vectors_and_their_positive_counterparts(text,positive,expected):
    assert declined_intents(text,'en')==expected
    assert declined_intents(positive,'en')==set()


@pytest.mark.parametrize('verb',['asking','requesting','intending','planning'])
def test_negative_progressive_request_predicates_are_service_specific(verb):
    assert declined_intents(f'I am not {verb} to check in today at 13:00.','en')=={'early_checkin'}
    assert declined_intents(f'I am {verb} to check in today at 13:00.','en')==set()
    assert declined_intents(f'I am not {verb} to wait for early check-in.','en')==set()


@pytest.mark.parametrize('language,negative,positive',[
 ('fr','Je ne veux pas de départ tardif demain à 13:00.',
  'Je voudrais un départ tardif demain à 13:00.'),
 ('es','No quiero una salida tardía mañana a las 13:00.',
  'Quiero una salida tardía mañana a las 13:00.'),
])
def test_native_late_checkout_aliases_require_explicit_refusal(language,negative,positive):
    assert declined_intents(negative,language)=={'late_checkout'}
    assert declined_intents(positive,language)==set()


@pytest.mark.parametrize('text',[
 'We do not refuse early check-in.', "We don't decline late checkout.",
 'Please do not wait for early check-in.', 'We refuse to wait for early check-in.',
 'I am not asking for parking; please check us in early.',
])
def test_new_markers_do_not_change_waiting_or_independent_parking_scope(text):
    expected={'parking'} if 'parking' in text else set()
    assert declined_intents(text,'en')==expected
