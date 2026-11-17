# Quick start

One page to get a Honey Badger (v0.4) running and safe. More detail: [SETUP](SETUP.md) (for whoever builds and
looks after the Badger) and [USAGE](USAGE.md) (for everyone in the family).

## What you have

- **The Badger**: a rugged box about the size of a big hardback book, with a small screen, a knob, a power
  button and a status light on the front ([TOUR](TOUR.md#front-panel)).
- **Antennas:** the folding weather-radio whip on the rear **SDR** jack (it folds into its channel), the small
  1090 MHz whip for aircraft on the **SDR 2** jack, and the 137 MHz V-dipole kit with 3 m of cable for weather
  satellites. Wi-Fi, LoRa radio and GPS antennas are built in.
- **Power cables:** a USB-C charger of your own (45 W or more, USB-C PD), and an adapter cable for each solar
  panel or battery you want to plug into the round **SOLAR / DC IN** socket.
- Built it yourself? Parts are in [BOM](BOM.md), printing in [PRINTING](PRINTING.md), assembly in
  [HARDWARE](HARDWARE.md), and installing the software in [SETUP](SETUP.md#2-install-the-software).

## 1. Power it

Use any one of these (you can connect two at once):

- **USB-C:** a USB-C PD charger (45 W+) or power bank in the rear USB-C socket.
- **Solar or 12/24 V battery:** into the **SOLAR / DC IN** (GX16 screw-lock) socket, 8–30 V. A solar panel plugs in
  directly; its open-circuit voltage (Voc on the label) must be **30 V or less**. Check a new cable's polarity
  with a multimeter first ([HARDWARE → Solar and DC input](HARDWARE.md#solar-and-dc-input)).
- **Its own battery:** the internal pack (about 50 Wh) runs it for roughly 7 hours, longer in Endurance mode.

Press the **power button**. After about a minute the status light goes **solid**: it's ready.

## 2. Join its Wi-Fi and open badger.lan

1. On the Badger, press the knob and choose **Join Wi-Fi (QR)**. The screen shows a QR code and the **whole
   Wi-Fi password** beside it (the Wi-Fi gets a random password when it is built).
2. Scan the QR code with your phone's camera, or pick the network from your phone's Wi-Fi list and type the
   password.
3. Most phones open the Badger's home page by themselves. If yours doesn't, open a browser and go to
   **http://badger.lan**.

Nothing needs installing, and nothing needs the internet. On a phone, *Add to Home Screen* makes it open like an
app ([USAGE → Phones, iPads and laptops](USAGE.md#phones-ipads-and-laptops)).

## 3. First-boot security checklist

Do this on day one, **before** the family's information goes on the Badger. It takes about ten minutes. The
admin page is **Settings → Badger administration**; open it at **https://badger.lan** so secrets aren't sent
over the Wi-Fi in the clear (your phone will warn about the certificate the first time; see
[USAGE → Using https](USAGE.md#using-https)).

1. **Admin passphrase.** The builder may have set a short admin PIN. Change it to a passphrase of a few words
   (10+ characters): at the Badger's console, `sudo prepperctl set-pin`
   ([SETUP → Admin passphrase](SETUP.md#admin-passphrase)).
2. **Sealed-records passphrase and recovery card.** *Sealed records* → set the owner passphrase (16+ characters
   or four or more words, not the admin PIN). The Badger shows a **recovery key** once: **print it or write it
   down** on your **recovery card**, keep the card away from the Badger, then tap *I've written it down*.
3. **Panel code.** Tap *Set up panel code*, then within 10 minutes at the Badger: knob → *System* → *Panel lock*
   → *Set a code*. Write its 12-digit recovery code on the recovery card too.
4. **Choose a posture.** Under *Security*, tap *Security posture*: **Guarded** is the default and right for most
   homes; **Neighbourly** helps neighbours more; **Lockdown** when danger is close.
5. **Duress secrets (optional).** Under *If someone forces you to unlock the Badger* you can set a duress PIN, a
   duress passphrase and (at the knob) a duress code. If someone forces you to unlock the Badger, a duress secret
   opens it normally, but every screen then shows a **made-up household** and the real records lock until the
   recovery key is typed (or, if you choose *Wipe*, are erased for good). Agree as a family first: **people come
   first** — if they could tell, give the real secret ([USAGE → Someone forces you to unlock it](USAGE.md#someone-forces-you-to-unlock-it)).
6. **Check "What this Badger reveals".** Under *Security*, tap *What this Badger reveals*. Fix anything amber
   with its one-tap button. Do it again once a month.

Then invite the family: *Family members* → *Invite a member* (or knob → *Invite member (QR)*). Each code works
once, on one phone, for 24 hours.

Your **recovery card** now holds the sealed-records recovery key and the panel recovery code. When someone sets
up the **Document vault**, add its recovery key too. Keep the card with your passports, not with the Badger.

## 4. Five things to try

1. **Ask the library.** Tap **💬 Ask the library** on the home screen and ask in plain words ("how do I purify
   creek water?"). The offline AI answers only from the Badger's own articles and lists its sources. It can be
   wrong, so check them.
2. **Open a map.** Tap **🗺️ Open map**: offline maps that work with no signal.
3. **Post on the check-in board.** Tap **📋 Check-in board**. *Community* is open to everyone nearby;
   *Family* is private to family members.
4. **Solve a problem.** Open **🧮 Problem solver** (under *More*) and give it a real problem with numbers ("a
   100 W panel, 4 hours of sun, a 12 V 100 Ah battery: how long will it run a 30 W fridge?"). It shows its working.
5. **Weather alerts.** Unfold the whip on the rear **SDR** jack. NOAA warnings then appear as a red banner on
   every phone and on the front screen. The builder sets your transmitter and county
   ([SETUP → SDR 1](SETUP.md#sdr-1-weather-radio)). The written forecast is under *Radio & sky → 📻 NOAA forecast*.

## Where next

- Everything the Badger does and who can use it: [USAGE](USAGE.md) and [FEATURES](FEATURES.md).
- What to do when something goes wrong (power cut, Lockdown, a lost phone, a forgotten passphrase):
  [USAGE → Emergencies](USAGE.md#emergencies).
- Setting up the radio, sensors, solar and updates: [SETUP](SETUP.md).
- The knob menu and every socket: [TOUR](TOUR.md). The full operating manual: [OPERATIONS](OPERATIONS.md).
- Turning it off: press the power button once, or knob → *System* → *Shut down*. Never just pull the power.
