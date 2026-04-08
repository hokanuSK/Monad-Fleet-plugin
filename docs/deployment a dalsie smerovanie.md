deployment a dalsie smerovanie
- deploy na fitku a dotestovat na ESP
- pridat aj bluethooth low advertisment
- nastavenie casovaca - tu porozmyslat o tom ako chceme casovat ake zariadenia

TODO projekt
agent loop tick - cron process 1s

urob nasledujuce zmeny na diagramoch. Takze kedze robime monad software nezaujima nas konkretne measurement window ale nazval by som to niaeco ako Execution Window a za nim pojde Maitanace window kde sa prave budu uploadovat artefakty. Chcem aby po kazdom vykonani jednotliveho commandu boli upravene metada v experiment aby bolo vidno stav vykonavania
New command_groups
- hello
- setMaitananceWindow
- ExecuteShell
- measure(WIFI, BLE, CSI)
- LogMetrics
- upload  
