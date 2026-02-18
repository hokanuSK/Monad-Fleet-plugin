# Analýza experimentu



## ANALÝZA: „WIFI SENSE“ (ČO, PREČO, AKO)

- Spraviť analytickú časť o „Wi-Fi sense“ v kontexte práce:
    - čo sa tým myslí v tomto projekte,
    - aké metriky/údaje sa budú zbierať,
    - prečo práve tieto metriky dávajú zmysel pre experiment.
    - Cieľ tejto analýzy: podložiť metodiku merania, nie len popísať, že „budeme merať“.

## HARDWARE REQUIREMENTS

- Zadefinovať hardvérové požiadavky pre meranie.
- Požiadavka na Wi-Fi kartu: dual-band karta 
- Vymedziť, aké vlastnosti musí mať HW

## POROVNANIE PRODUKTOV NA TRHU

- Urobiť porovnanie dostupných riešení / produktov na trhu:
    - porovnať relevantné Wi-Fi adaptéry / karty (z pohľadu merania),
    - určiť, ktoré riešenie je najvhodnejšie pre cieľ experimentu.
    - Výstup: zdôvodnený výber HW a spôsobu merania („ako najlepšie robiť merania“).

## „FILOZOFIA“ / KONCEPCIA EXPERIMENTU

„Pohrať sa s filozofiou experimentu“ = jasne popísať:
    - čo je cieľ merania, prečo sa meria, aká je logika experimentu (čo považujeme za úspešné/validné meranie).
    - Nastaviť experiment tak, aby bol prakticky realizovateľný (celodenné behy) a aby dáta dávali zmysel pre ďalšie spracovanie.

## SYNCHRONIZÁCIA

- interval synchronizácie (ako často sa a hlavne kedy sa synchronizuju dáta).
- Určiť, ako sa bude riešiť synchronizácia počas dlhého merania (celé dni).
## VZORKOVANIE MERANIA

Otázka: nechceme zaviesť vzorkovanie merania (sampling)?

- Zvážiť, či je potrebné merať nepretržite alebo merať v intervaloch alebo niaky adaptivny rezim podla prave nameranych metrik(aktivita RSSI) - zvoliť vhodnú frekvenciu merania

- Z toho odvodiť odporúčaný režim merania pre experiment.

## OBJEM DÁT PRI CELODENNOM MERANÍ

- Kľúčová otázka: koľko dát sa bude generovať, ak meranie beží celé dni.

- Urobiť odhad dátového objemu:
    - podľa zvolených metrík,podľa frekvencie merania, podľa formátu ukladania.
- Vyhodnotiť, na zakladne moznosti s predchadzajucich kapitol, či je objem dát udržateľný pre ukladanie (napr. SD karta) a následné spracovanie.

## Umiestnenie ZARIADENÍ (POLOHA MERACÍCH BODOV)

- kde majú byť zariadenia, ktoré merajú.
