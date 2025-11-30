

## TODO
###  analyza 
- analyza plugin systemu 
- zistit co vsetko robia veci v GUI  compounds a steps. 
- doriesit ci vyuzivame vsetky moznosti elabftw pre nase potreby
- analyza datovych schem
  - vediet ako funguju vztahy medzi tabulkami a kde sa co vytvara vzhladom na GUI
- anylaza API - moznosti integracie

### navrh 
- namapovanie elabFTW moznosti na nas usecase
- namapovanie datovych schem na nas usecase 
- budu resources predstavovat jedno zariadenie alebo viac?
- custom field pre resources - mac adresa
  
### Implementacia
- postavit "plugin" s grpc a funkcionalitou pingu na zariadenie. zariadenie sa zobudi a ozve sa GRPC serveru. 
 funckia grpc bude mat macadresu ako vstup.ak zariadenie sa uz ozvalo vratim kedy sa ozvalo a ak neexistuje tak ho vytvorim. Last seen at polozka v tagu?

## question
- merane data - co su to za data? 
  - data s merani su binarne data a nevieme zatial ci pojdu do elabftw


# analyza
## Datova struktura
## Navrh 



# Monad Fleet Plugin For ElabFTW platform
- this plugin will be able upload whole infrastrucutre define as docker images into device
## Example Device Nodes

### Rasberry Pi with Dual band wifi
- 2,4 GHz interface will be used for comunication, mesaurement will be done only on 5Ghz interface.
- GRPC client
- Prometeus for loging


