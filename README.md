# Malmars Local

**Everything local. Unless you want it.**

I'm not an engineer. I'm an artist with an engineering heart, and I build
local-first game tools that a kid - or anyone on the spectrum - can
understand and run without help. Twenty years of starting things and never
finishing them ended when my twin sons Malcolm and Marcus, mathematical
geniuses obsessed with animation and Roblox, gave me the rule I now build
by: if I build a game asset maker, it has to be something a child could
run without help. So everything local - your machine, your files, your GPU,
no cloud unless you want it - one download, one button, works the first
time, and free stays free. That is what the station below is, and the same
brief is growing into a toy company, HERBERTO MALMARS, spectrum-friendly
toys printed at home. I build it between school runs and homework, which
is also where I am stuck: I do not have the hardware to finish the job. I
need a multi-colour 3D printer (Prusa MK4S + MMU3, Bambu X1C/P1S + AMS, or
a Prusa XL), because my printer is single-colour and the multi-material
print files stay untested until one of those exists; a 24 GB NVIDIA card
(3090, 4090 or 5090), so the larger local image model does not need a
cloud service; and spare old CPUs and RAM (server pulls, Xeon, 64 GB+), so
the studio can run on machines that cannot afford a graphics card. If you
can donate or lend any of that, open an issue or write to
beta@jherbalz.studio - you get the free beta forever, your name in the
credits and in the app, your machine as a test runner whose compatibility
lands in the public issue tracker, and a say in the roadmap.

---

I build 3D character tools that run entirely on your own computer.
Nothing you make leaves your machine, there is no account, and there is no
subscription to anything.

I do not have the hardware I need to finish the job. This page is the
list.

## What I need

| machine | what it unlocks |
|---|---|
| multi-colour 3D printer (Prusa MK4S + MMU3, Bambu X1C/P1S + AMS, Prusa XL) | the multi-material print files. My printer is single-colour, so the colour lane is untested until one of these exists |
| a 24 GB NVIDIA card (3090 / 4090 / 5090) | the larger local image model, so a good sheet does not need a cloud service |
| spare old CPUs and RAM (server pulls, Xeon, 64 GB+) | running the studio on machines that cannot afford a graphics card, and testing the screen model on CPU |

If you can donate or lend any of these, open an issue or email
beta@jherbalz.studio.

## What you get back

- The free Print Station beta, always.
- Your name in CREDITS.md and in the app's About panel.
- Your machine becomes a test runner. Your printer and your card become a
  compatibility entry in the issue tracker, which is how the next
  person's setup gets known to work.
- A say in the roadmap. The changelog is public and issues are triaged.

## What is here

- `print_station/` - the free Print Station source. Slicer detection,
  printer profiles, G-code facts, model library, print file generation.
- `safety/` - the content screen, in full. A local CPU classifier and a
  prompt list that block adult content before it reaches the generator or
  the printer. Read it; it is short.
- `print_station/test_print_station_discovery.py` and the two files in
  `safety/` that start with `test_` - the tests. They run on
  every push on a normal GitHub runner, and they are the proof that the
  switch works and that the screen fails closed.
- `HARDWARE-WISHLIST.md` - the three machines that unblock the work.
- `CREDITS.md` - the hardware donors.

The screen needs one image classifier. It is not in this repo and it is not
for sale: the app fetches it from the publisher of the model, and the hash it
expects is written into the source, so anyone can see exactly which file it
wants.

## Also free and public now

The other tools are separate repos, also free, also local, also MIT:

- [Sprite Turntable](https://github.com/jherbalz/sprite-turntable) - turns a 3D
  model into a flat sprite sheet of 16, 32 or 64 angles. Blender only. No
  weights, no network, one button.

## Licence

The Print Station source is under the terms in `LICENSE.md`. The safety
module is part of the same project. Model weights are not included and
are not for sale here - if an app fetches weights it fetches them from
their publishers and their licences apply, not ours.
