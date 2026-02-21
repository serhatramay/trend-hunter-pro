# Trend Hunter Pro

Google News + Google Trends sinyallerinden anahtar kelime bazlı erken trend yakalama dashboard'u.

## Calistirma (Lokal)

```bash
cd trend-hunter-pro
python3 server.py
```

Tarayicida ac:

- http://127.0.0.1:8080

## Canli Veri

- Taramada Google News RSS uzerinden haberler cekilir.
- Google Trends RSS uzerinden trend sinyali alinip skorlamaya katilir.
- Her taramada yeni haberler veritabanina islenir, skorlar guncellenir.

## Ozellikler

- Anahtar kelime ekle/sil
- Manuel ve otomatik tarama
- Google News RSS tarama
- Google Trends RSS sinyali
- Erken trend skoru (0-100)
- Kaydedilen haberler
- Filtreleme ve istatistik paneli
- Haber kartlari tiklanabilir (Aç butonu + kart tiklama)

## Ucretsiz Deploy (Render)

Bu klasoru ayri bir GitHub reposu olarak push etmen yeterli.

1. Bu klasorde yeni repo olustur:

```bash
cd trend-hunter-pro
git init
git add .
git commit -m "Initial Trend Hunter Pro"
```

2. GitHub'da bos repo acip bagla:

```bash
git branch -M main
git remote add origin <GITHUB_REPO_URL>
git push -u origin main
```

3. Render'da:

- New + -> Blueprint
- GitHub repo sec
- `render.yaml` otomatik okunur
- Create Blueprint de

Render URL olusunca uygulama canli olur.
