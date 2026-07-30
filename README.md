# KlipperMQ

Klipper is excellent at coordinated multi-tool printing—until you need toolheads that do **not** share one mirrored or copied motion plan. KlipperMQ is an experimental, additive fork aimed at real multi-queue motion: independent tool paths, overlapping park/unpark during toolchanges, and a recovery path built for multi-extruder machines—without throwing away Klipper’s host/MCU model or its broad hardware support.

The design goal is a **clean superset** of stock Klipper. Single-toolhead configs should keep working with little or no change. Classic `dual_carriage` / IDEX setups remain valid; new `[queue]`, toolchange, and recovery options opt you into multi-queue behavior when you want it. Configuration stays text-file oriented, MCU firmware changes stay minimal for v1, and the motion path prefers ordinary timed steps plus deterministic host bookmarks rather than a ground-up protocol rewrite.

KlipperMQ is **early / incomplete**. Ownership, planning hooks, thin `TOOLCHANGE` orchestration, and host-side recovery foundations exist; concurrent dual-trajectory emission, full COPY/MIRROR parity with every dual_carriage workflow, and hardened on-printer validation are still in progress. Treat this as a development tree for builders who want more freedom in multi-toolhead motion and are willing to test, break things, and report what actually fails on real hardware.

If you want “stock Klipper but my second toolhead can park while the first keeps printing—and I can resume a dual-tool job after a power hit,” that is the north star. Contributions and harsh feedback from multi-toolhead users are welcome; please read `ARCHITECTURE.md` before large changes.
