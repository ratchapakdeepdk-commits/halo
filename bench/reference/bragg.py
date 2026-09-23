import math
def d_spacing(a, h, k, l):
    if a <= 0 or h == k == l == 0: raise ValueError
    return a / math.sqrt(h*h + k*k + l*l)
def two_theta(d, wavelength):
    if d <= 0 or wavelength <= 0 or wavelength / (2*d) > 1: raise ValueError
    return 2 * math.degrees(math.asin(wavelength / (2*d)))
def allowed_fcc(h, k, l):
    p = {x % 2 for x in (h, k, l)}
    return len(p) == 1 and not h == k == l == 0
def first_peaks_fcc(a, wavelength, n=5):
    seen, out = set(), []
    cands = sorted(((h*h+k*k+l*l), (h, k, l)) for h in range(0, 12) for k in range(0, h+1) for l in range(0, k+1))
    for s, hkl in cands:
        if s in seen or not allowed_fcc(*hkl): continue
        seen.add(s)
        try: out.append((hkl, two_theta(d_spacing(a, *hkl), wavelength)))
        except ValueError: continue
        if len(out) == n: break
    return out
