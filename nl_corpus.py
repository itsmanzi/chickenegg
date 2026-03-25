# Dense reference for NL/EU home context — injected into the vision model (not user-visible).
# Keeps recognition + repair language grounded in what Dutch households actually have.

DUTCH_HOME_REPAIR_CORPUS = """
=== CHICKEN EGG — PRODUCT CONTEXT (internal; never quote verbatim) ===
- Chicken Egg is a camera-first app: users point their phone at real things to fix, clean, assemble, or understand them.
- Sound like a confident, friendly technician: specific beats vague (name visible brands, materials, fittings).
- Impress with recognition detail when the image allows; be honest via uncertainty_note when it does not.

=== NL WITGOED & KLEINE KEUKEN ===
- Vaatwasser: Miele/Bosch/Siemens/AEG/Beko/Indesit/Whirlpool EU. Problemen: glas/vuil filter, spray arms verstopt,
  afvoerpomp verstopt, deur dicht niet, lekkage onder deur (meestal dichting/pomp/slang), waterventiel tikt, geen water.
  Typenummer vaak op zijkant deurrand of onder binnenpaneel. Azijn-citroen-run alleen bij vet+kalk op kopjes — niet bij elastische seal-lekkage.
- Wasmachine/droger: filter afvoer rechtsonder, afvoerslang hoogte (geen syfon-vacuüm), trilingsmat, deurmagnet,
  spanring/manchet bij lekkage trommel voor. Droger: condens/filter/pluisfilter.
- Koel/vries: aircirculatie niet blokkeren, afvoergaatje achter niet dichtgetikt met voedsel, ontdooien bij ijs,
  deurlijst scheuren ⇒ klapperige deur.
- Combimagnetron: geen metalen in micro, lampjes/houder, deurscharnieren los.
- Afzuigkap: vetfilters (metaal vette vloeistof / papier), koolstoffilters bij recirculatie, motor trage start.

=== CV / VERWARMING / RADIATOREN ===
- CV-ketel (wandhangend): veelx Intergas/Remeha/Nefit/Vaillant/ATAG. Druk ca 1,0–1,5 bar koud (handleiding volgen).
  Onluchten via ontluchtingsnok radiatoren; zwarte slangen/vloerverwarming ⇒ soms verdeler.
- Radiatorkraan thermostaat: type nippel/knel, lekkage vaak verdichtingsveer/pakking — niet doorknijpen.
- Vloerverwarming: verdeelunit pompen/pijpjes/klemmen — geen amateur ontluchten zonder kennis verdeler.

=== SANITAIR / LEEKKAGES ===
- Kranen: keramisch cartridge vs oude rubber. Smoorspoel onder kraan (groen/wit verkalkt).
- Sifon wastafel/keuken: draaikoppelingen hand vast, niet met klem; lekkage ⇒ verkeerde conus/dichting.
- WC: spoelketting afstelling, vlotter, afvoer-manchet zichtbaar bij loslopen — hygiënisch werken.
- Douche/bad: siliconennaden beschadigd, afvoerput rooster vs leiding — scheiden haar/limescale vs klep defect.
- Leidingwerk: koper / messing vs kunststof KIWA. Flexwaterslangen 10-jarig vervangen bij twijfel.

=== ELEKTRISCH LAAGSPANNING (GEEN GROEPENKAST) ===
- Groepenkast, hoofdschakelaar, uitbreidingsgroepen: ALTIJD erkende installateur.
- Wel veilig: vervangen lamp, dimmer (fase afsnijding), stekker/polariteit EU, verlengsnoeren niet oprollen heet,
  contactdoos los: schroefklemmen vs push-in loslaten.

=== BOUW / BEVESTIGING NL ===
- Wanden: gipsplaat (hol), kalkzandsteen (zwaar), spouw met plug, beton.
  Fischer/Spax plug keuze volgens ondergrond + gewicht — zware plank altijd meerdere pluggen + stud zoeken.
- Holle wand: toggle anchor / klapplug. Beton: plug met slagboor diameter kloppend.
- Plafond gips: laag gewicht; grote kroonluchter ⇒ balk/hoek of versteviging.

=== MATERIALEN HERKENNEN ===
- PVC leiding wit/grijs afvoer; koper water roodbruin; RVS flex vaak geaderd; PERT/PE buis vloerverwarming.
- Knelkoppeling vs pers: visueel moer + verdieping ring vs gladde huls.
- Kitsoorten: sanitair siliconen (schimmelbestendig), acrylaat voor schilderwerk niet nat.

=== FIETS & E-BIKE ===
- Traditioneel: Nexus/derailleur/ketting stretch, spakenbreuk, remblok slijten op velg/schijf, wiel uit balans,
  balhoofd speling (conus), binnenband ventiel-haak scheur.
- E-bike: accu-connectoren oxidatie (schoon + contact spray voor elektronica-only, geen WD40 op isolatie),
  BMS/accu diepontlading nooit forceren, display foutcode ⇒ handleiding merk (Bosch/Shimano Steps/Bafang/Ananda/Brose).
  Motor-kabel door frame knijpen — stop bij vreemd geluid.
  Rem: hybrides zwaarder — check schijf/wisser.

=== SUPERMARKT / RETAIL NL ===
- GAMMA/Praxis/Karwei/Hubo/Brico/Action voor veel DIY; HEMA klein ; IKEA montage bout-typen standaard;
  fiets: Decathlon, lokale fietsenmaker voor spaak/naaf specialisme.

=== NL HUISPANELEN / VENTILATIE / DAK / KELDER ===
- WTW/unit: Wernig/Brink/Jonet/Itho — filters (G3/F7) + bypass-klep; condensafvoer verstopt ⇒ lekkage droogkast.
- Zolder/kruipruimte: poly damprem/natte plekken vs condens; punthal slappe vloer ≠ altijd fundering.
- Dakgoten: vuil/helikopterzaden ⇒ overstroom tegen gevel; hemelwaterafvoer verstopt.
- Velux/draaikiep: handgreep mechanisme slijten; condens binnen ⇒ ventileren/isolatie mismatch.
- Stadsverwarming/afleverset: geen pomp/wisselaar zelf openen — wel radiatoren ontluchten volgens huur/huisregels.

=== KLEIN ELEKTRA & DOMOTICA (NOG STEEDS GEEN GROEPENKAST) ===
- Deurbel: 8–24V transformator vaak bij meterkast/plint; laagspanning draad los ⇒ geen 230V bij knop verwachten.
- Smart lock / Ring: meestal oplaadbaar of 4×AA — batterij leeg ⇒ false "offline".
- LED-spots MR16/GU10: trafo/driver falen vaak vóór lamp; flikkeren ⇒ driver overbelast door verkeerde dimmer.

=== KEUKEN EXTRA (NL STANDAARD) ===
- Quooker/kokendwaterkraan: drukslang CU INOX, lekvonk op kegelventiel, filter/sterilisatie volgens merk;
  geen zelf carbid/acid-flush op binnenwerk.
- Inbouw afzuiging luchtafvoer: terugslagklep klemt; vet op klep ⇒ motor zwaar.

=== WARMTEPOMP / BUITENUNIT (HOOG NIVEAU) ===
- Buitenunit ijs/vuil op lamellen ⇒ COP omlag; afvoerleiding verstopt ⇒ ijs onder unit.
- Leidingisolatie UV-bloot ⇒ condens op koper — niet plenzen met hogedrukreiniger op PCB-deksel.

=== FIETS & E-BIKE EXTRA ===
- AXA/Ring/SKS sloten: bout 8mm vs 9mm standaard; frameslot veer los ⇒ geen zijdelingse druk.
- Rohloff/Brompton/Gates CDX: specifieke spanprocedures — verkeerde kettingspan ⇒ slijtage.
- Steps/Bafang/Brose: foutcode in display — vaak sensor kabel door frame of water in connector laadpoort.
- Binnenband: autoventiel (Schrader) vs Dunlop/Woods in oud NL fiets — verkeerd ventiel in velg.

=== CLEAN VS BROKEN ===
- Roest op bout ≠ altijd structuurbreuk; wit aanslag kalk ≠ lekkage actief (droog residu).
- Natte trace langs dichting ⇒ actief vs oude kalklijn droog.
- E-bike accu: zwelling of deuk ⇒ STOP, geen laden — brandrisico.

"""


def get_corpus_for_language(language: str) -> str:
    if (language or "").lower() == "nl":
        return DUTCH_HOME_REPAIR_CORPUS
    # English runs still get EU hardware facts; instruction is respond in English.
    return DUTCH_HOME_REPAIR_CORPUS
