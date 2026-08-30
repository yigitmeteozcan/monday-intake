# Komutlar / Commands

Üç şey var. Hepsi bu.
Three things. That's all.

| Komut | Ne yapar | What it does |
|---|---|---|
| `!addp isim!` | **Girişim** ekler → deal board | **Startup** → deal pipeline |
| `!addi isim!` | **Yatırımcı adayı** ekler → investor board | **Investor lead** → investor board |
| `!note yazi!` | Ayrı bir not olarak ekler | Posts as its own separate update |

---

## Altın kural / The golden rule

**`!` ile başla, `!` ile bitir.**
**Start with `!`, end with `!`.**

```
!addp hockey!
!addp Larkspur AI!
!addi Northwind Co!
!note startup güzelmiş!
```

Kapanış `!` işareti ismin nerede bittiğini söyler. Böylece konu satırının
ortasına da yazabilirsin, karışmaz.
The closing `!` tells the script where the name ends, so you can write it in
the middle of a sentence and nothing gets confused.

**İki `!` arasına ne yazarsan aynen o gider.** Büyük/küçük harf, noktalama,
boşluk — hiçbiri değiştirilmez.
**Whatever you put between the two `!` is exactly what lands on the board.**
Case, punctuation, spacing — nothing is changed.

```
!addp ÇOK Güzel A.Ş.!   →  ÇOK Güzel A.Ş.
!addp Hero deck!        →  Hero deck
!addp hwatEver!         →  hwatEver
```

```
FW: bi girisim var !addp Hero! bakar misin !note cok iyi duruyor!
→ item: "Hero"  +  ayrı not: "cok iyi duruyor"
```

---

## İsim nasıl belirlenir / How the name is chosen

1. **`!addp isim!` yazdıysan** → o isim. Başka hiçbir şeye bakılmaz.
   You typed it → that's the name, full stop.
2. **Sadece `!addp` yazdıysan** → mailin konusu isim olur.
   Bare `!addp` → the email subject becomes the name.
3. **Konu düz cümleyse** → hiçbir şey eklenmez, sana "ismi yaz" diye mail döner.
   If the subject is a sentence → nothing is added, you get a reply asking for the name.

```
!addp hockey!                      → hockey
FW: Application - ACME !addp      → ACME
FW: bunu ekler misin !addp         → ✗ eklenmez, isim sorar
FW: bunu ekler misin !addp Hero!   → Hero  ✓
```

**Emin değilsen ismi yaz.** `!addp Hero!` her zaman çalışır.
**When in doubt, type the name.** `!addp Hero!` always works.

---

## Notlar / Notes

- **Komutları nereye yazarsan yaz çalışır** — konuya, mailin en üstüne, ya da
  yönlendirilen yazının ortasına. Hepsi bulunur.
  Commands work **anywhere**: the subject, the top of the mail, or buried in the
  middle of the forwarded text.
- **`!note` tek başına hiçbir şey yapmaz.** Mutlaka `!addp` veya `!addi` lazım.
  `!note` on its own does nothing — you always need `!addp` or `!addi`.
- Birden fazla not yazabilirsin: `!note bir! !note iki!` → iki ayrı not.
  Multiple notes become multiple updates.
- Türkçe karşılıkları: `!ekle` `!girisim` `!startup` = `!addp` ·
  `!yatirimci` `!lead` = `!addi` · `!not` `!yorum` = `!note`
- Komut yoksa **hiçbir şey olmaz**. Normal maillerin Monday'e gitmez.
  No command means nothing happens.
- Aynı şirket zaten varsa yeni kayıt açılmaz, mail mevcut kaydın altına eklenir.
  If the company already exists, the email is added to it instead of duplicating.
- Her işlemden sonra Monday linkiyle onay maili gelir.
  You get a confirmation reply with the Monday link.
