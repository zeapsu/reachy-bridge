# User manual

How to use the reachy-bridge dashboard, and why it behaves the way it does. To install the
bridge, see the [README](../README.md).

- [The dashboard](#the-dashboard)
- [Power](#power)
- [Who controls the robot](#who-controls-the-robot)
- [Expressions and driving](#expressions-and-driving)
- [Motors and kinematics](#motors-and-kinematics)
- [Voice](#voice)
- [Apps](#apps)
- [Simulation](#simulation)
- [Devices and logging in](#devices-and-logging-in)
- [Family profiles](#family-profiles)
- [Troubleshooting](#troubleshooting)

## The dashboard

The dashboard has three tabs:

- **Robot:** the live view of the robot, with panes for Expressions, Drive, Controls and Voice.
- **Apps:** installed apps, and Get apps to install more from Hugging Face.
- **System:** the board's stats, logs, paired devices, simulation and appearance. Once the
  dashboard has an admin, System is the admin's only: other people see it dimmed with a lock (see
  [Family profiles](#family-profiles)).

The top bar always shows the robot's status and the power button. The status shows the running app
or, with a person icon, who is in control. While anyone has an audio session open, a badge shows
one icon per live route (ear: surroundings, speaker: Reachy's voice, mic: out loud, speech bubble: to
apps) and their name; hover it, or use a screen reader, for the full sentence. A Release button
appears while you are in control.

## Power

The robot starts off. The bridge keeps running while it is off, so you can reach the dashboard
at any time and switch the robot on from there: while it is off the robot shows dimmed and asleep,
with a Power on button on the stage (Wake when it is asleep).

| State | Meaning | What you can do |
|---|---|---|
| Off | The motors have no power. | Power on. |
| Starting | The robot is waking up. | Wait. |
| On | The robot is awake. | Everything. |
| Asleep | The robot is powered but limp, usually because an app just ended. | Power on to wake it. |
| Robot service down | The service that runs the robot (the daemon) isn't answering. | See [Troubleshooting](#troubleshooting). |

Most controls need the robot on, so they are greyed out in the other states. Expressions,
driving and controls come back once it is on. Starting an app wakes the robot if it is asleep; if
it is off, power it on first.

## Who controls the robot

This applies once your dashboard has an [admin](#devices-and-logging-in). Without one, anyone who
reaches the dashboard can drive.

One browser at a time drives the robot. Your first robot action takes control if nobody has it,
and the dashboard tells you when it does. Control ends when you press Release, when you close the
dashboard, after 10 minutes of doing nothing with the robot, or after a minute without contact
from your browser. It also ends when the admin takes over or revokes your browser. A browser that
went quiet gets control back if it returns within 5 minutes, unless someone else took it. Losing
control ends your drive session and stops a playing expression, but a running app keeps running.

If someone else is in control, the panes show who it is. You can drive once they let go. The admin
can always take over.

If you are in control on another of your own devices, an action on this one moves control here.

## Expressions and driving

Expressions play a recorded emotion or dance. They are unavailable while an app is running,
because the app owns the robot, and only one can play at a time.

Drive moves the robot by hand. The Head pad looks around. The Body pad turns when you drag
sideways, and raises or lowers the head when you drag up or down. The sliders move the antennas.
The pin (Hold pose) keeps the pose after you let go. Home centres the controls and ends the drive
session, and the robot then returns to its rest pose. Driving needs the motors enabled, and it is unavailable while an app is running or a move is finishing. When motors or an app are what's in the way, the Drive pane offers Enable motors or Stop app.

## Motors and kinematics

**Motors** are Enabled or Off. With them off, the robot goes limp and can drop its head; set them
back to Enabled to recover.

**Gravity compensation** lets you move the head by hand. It needs the Placo kinematics engine. It
is off while an app runs, because the robot ignores moves under gravity compensation.

**Kinematics** picks the engine that turns poses into motor positions. Only the admin can switch
it, and not while an app runs. Switching with the robot on powers it off and back on; otherwise the
change takes effect at the next power-on. An engine that isn't installed on the robot can't be selected.

**Audio-reactive wobble** moves the head with whatever plays through the speaker: the test sound,
an app, or your voice.

## Voice

The Voice pane connects your device's audio to the robot. Each direction can be switched while
connected.

| Chip | What it does |
|---|---|
| Listen: Surroundings | Plays the robot's microphone on your device. |
| Listen: Reachy's voice | Plays the robot's voice, an app's voice and its sounds on your device instead of on the robot. |
| Talk: Out loud | Plays your microphone through the robot's speaker. |
| Talk: To apps | Sends your microphone to what apps hear as the robot's microphone, so a conversation app hears you as if you were next to the robot. |

Talking through the speaker doesn't reach apps, because the robot's audio board cancels its own
speaker output from its microphone. Anything else reading the robot's microphone, such as a remote
session connected through the daemon, also hears you when you talk to apps.

**Your mic level.** While you talk, the lit Talk chip fills from the left with your microphone's
level. If it stays flat while you speak, the robot isn't getting your voice.

**Picking a microphone.** While Out loud or To apps is on, a row under the Talk chips shows the
microphone your browser will send, so you can pick another before pressing Start. While talking,
picking switches at once without reconnecting; the choice is kept until you leave the page. On a
first visit the browser doesn't share microphone names until talking starts, so the row shows up
then. The row turns amber with a crossed-out mic if no sound at all has come from the microphone
since it started (a dead or muted input). A wrong microphone that still hears the room, such as a
Bluetooth speaker's, isn't flagged, so check the name.

**Privacy.** Nothing in the room signals that someone is listening. Every dashboard shows a mic
badge with the person's name while a session is open.

**What you need:**

- The robot on or asleep, and the real robot rather than the simulation.
- The dashboard's Tailscale address. Audio can't connect from a home-network address.
- The https address to talk, because browsers only share a microphone with a secure page.
  Listening also works on the plain-http tailnet IP.
- Control of the robot, if the dashboard has an admin.

Only one browser can have a session. If someone else has one, Start reads Take over audio, and pressing it ends theirs.

**A session ends** (the pane says Audio ended) **when:**

- the robot is turned off;
- control or your login changes;
- the robot's audio stops;
- another browser starts a session.

**Apps that take the hardware.** An app that claims the camera and microphone for itself makes
them unavailable to the dashboard until it stops. The bridge takes them back when the app's process
ends.

## Apps

Apps come from Hugging Face Spaces and run on the device as the bridge's own user, so install only
apps you trust. Only the admin installs, updates and removes them, and sets the Hugging Face token.

Start an app from the Apps tab. One app runs at a time, and it owns the robot while it runs, so
expressions, driving and gravity compensation are unavailable. You can't start an app while an
expression is playing or a drive session is open. Pin up to 3 apps to keep them at the top of the list. Manage shows updates and removal;
stop an app before removing it.

An app with its own interface opens in the dashboard, and that interface can also open in a new
tab. When an app ends by itself or crashes, the robot goes to sleep. When you stop it, the robot
stays awake.

A Hugging Face token is only needed for private Spaces. Create a read token at
huggingface.co/settings/tokens and sign in under Get apps. The robot keeps the token, and the page
doesn't show it again.

## Simulation

System › Simulation lets the dashboard drive a simulated Reachy. The real robot stays off until you
switch back. To enter, it must be off with no app, expression or drive session running. Starting
the simulation can take up to a minute and a half.

The simulated robot has its own apps. It has no camera or Voice pane audio, and its sounds can play
on your device instead. A Sim badge in the top bar shows when you are driving it.

## Devices and logging in

**Admin.** Until an admin exists, anyone who reaches the dashboard can drive the robot. Under
System › Devices, choose Make this browser the admin, then run the command it shows on the robot.
Only the command on the robot can approve an admin. After that, every browser logs in, including
over Tailscale.

**Adding a browser.** A new browser shows a 6-digit code, valid for 5 minutes. Approve it from an
admin's dashboard, or run `reachy-bridge pair <code>` on the robot. Too many wrong tries lock the
code, so only the command on the robot can approve it.

**Removing a browser.** Revoke it under Devices, or run `reachy-bridge devices revoke <n>`. A browser
unused for 30 days is logged out.

**Shared browsers.** Turn on Shared when approving a family tablet. It belongs to nobody: whoever
picks it up chooses their profile (see below).

## Family profiles

Once the dashboard has an admin, everyone who uses it has a profile: the person each browser was
approved for. Only the admin sees System, and only the admin manages profiles, under System ›
Profiles. Everyone else has a chip with their name in the top bar. Tap it for the browser's
appearance and to log this browser out.

**What a profile can do.** Each profile is a Grown-up or a Kid, with:

- **Apps:** all installed apps, or the ones you switch on. The Apps tab lists only those, and the
  robot refuses to start, stop or open any other. Starting an app wakes the robot, even for a profile
  that can't use the power button.
- **Power on and off**, **Drive and expressions**, **Talk and listen**, and **Camera**: anything
  switched off is dimmed in the dashboard and refused by the robot.
- **Kid** profiles also lose the Controls pane (volume, sounds and motor mode).

People approved before profiles existed are Grown-ups with everything allowed, so nothing changes for
them except System. A new profile starts as a Kid with no apps, power and driving on, and Talk and
camera off. Changes apply within seconds. A profile's open camera, voice or live views reconnect
under the new rules. Each profile keeps its own pinned apps. The admin's profile always has
everything, and can't be restricted.

**Adding someone.** Add a profile, then approve their browser with their name as the person. To
move a browser to another profile, log it out and approve it again. A profile with browsers of its own
can't be removed: log them out first. Renaming a profile moves its browsers with it.

**Shared tablets.** A browser approved as Shared shows everyone's profile when it opens. Pick yours.
Admins are shown dimmed with a lock, because an admin's profile works only on the admin's own
browsers. Until someone picks, the tablet can only watch. Switch in the chip's sheet hands it back
to the picker, which ends that person's control and voice session. Anyone can pick any non-admin
profile on a shared tablet, so don't share one between people with different rules if that
matters. After the robot restarts, the tablet asks again.

## Troubleshooting

| What you see | What to do |
|---|---|
| Robot service down | Run `reachy-bridge status`, then `reachy-bridge logs daemon`. |
| The dashboard won't load on an iPhone by its tailnet name | Turn off iCloud Private Relay, and Limit IP Address Tracking for the network. |
| Voice chips are greyed out | Check [what you need](#voice). |
| Voice stops by itself | See [when a session ends](#voice). |
| Talk is on but the robot is silent, and the Talk chip doesn't fill when you speak | Your device is using another microphone, or it's muted. Check the microphone named under the Talk chips and pick another. If none works, check the input in your computer's sound settings (Safari on a Mac always uses the system's default input), the browser's site permissions, and any mute switch on a headset. |
| The mic shows as unavailable | An app has taken the hardware. Stop it. |
| An app's interface won't open | Run `reachy-bridge logs controller`. |

`reachy-bridge check` repeats the device check, including the audio parts. System › Logs shows the
same logs in the dashboard.
