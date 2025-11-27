# Discord Bot

## Instalace pro windows

1. Klonuj repozitář:
   git clone https://github.com/Undeadprogramming/IC2.git

2. Vytvoř virtuální prostředí:
   python -m venv venv  
   .\venv\Scripts\Activate.ps1

4. Nainstaluj knihovny:
   pip install -r requirements.txt

5. Vytvoř soubor `.env` a vlož token:
   DISCORD_TOKEN=tvuj_token_z_discord_portalu  
   echo "DISCORD_TOKEN=tvuj_token" > .env

7. Spouštění bota
   python main.py [parametry]  

   příklad: python main.py --mode active --pick-channels --pick-users --scan-limit 200 --history-limit 100
