# Device photos

Artifact pages cannot load an image from an external host — the content
security policy blocks every non-allowlisted origin, silently, so a hotlinked
vendor photo renders as a gap rather than an error.

To add a real photo of a device:

1. Save the file here, e.g. `igs-4215.jpg`. Keep it under ~1 MB.
2. Set `image="images/igs-4215.jpg"` on that model's entry in `devices.py`.
3. Re-export and republish:
   `sitemap.py export sites/*.yaml -o ui/site-data.js`

Use images you have the right to use — a vendor datasheet you were sent, or a
photo taken at the bench. A photo of the unit as it is actually installed is
worth more than a catalogue shot, because it shows the cable run.

The drawn faceplate is not a placeholder waiting for a photo. For the socket
question — "which one is gi8" — it is the better answer, and it stays correct
when a vendor refreshes their product photography.
