# (c) 2015-2016 Acellera Ltd http://www.acellera.com
# All Rights Reserved
# Distributed under HTMD Software License Agreement
# No redistribution in whole or part
#
from __future__ import print_function

from htmd.home import home
import numpy as np
import os
import os.path as path
from glob import glob
from htmd.molecule.util import _missingSegID, sequenceID
import shutil
from htmd.builder.builder import detectDisulfideBonds
from htmd.builder.builder import _checkMixedSegment
from subprocess import call, check_output, DEVNULL
from htmd.molecule.molecule import Molecule
from htmd.builder.ionize import ionize as ionizef, ionizePlace
import logging
logger = logging.getLogger(__name__)


def listFiles():
    """ Lists all available AMBER forcefield files
    """
    try:
        tleap = check_output(['which', 'tleap'], stderr=DEVNULL).decode('UTF-8').rstrip('\n')
    except:
        raise NameError('tleap not found. You should either have AmberTools or ambermini installed '
                        '(to install ambermini do: conda install ambermini)')

    amberhome = path.normpath(path.join(path.dirname(tleap), '../'))

    # Original AMBER FFs
    amberdir = path.join(amberhome, 'dat', 'leap', 'cmd')
    ffs = [f for f in os.listdir(amberdir) if path.isfile(path.join(amberdir, f))]
    print('---- Forcefield files list: ' + path.join(amberdir, '') + ' ----')
    for f in ffs:
        print(f)
    # Extra AMBER FFs on HTMD
    htmdamberdir = path.join(home(), 'builder', 'amberfiles', '')
    extraffs = [f + '/' + path.basename(glob(os.path.join(htmdamberdir, f) + '/leaprc.*')[0])
                for f in os.listdir(htmdamberdir) if os.path.isdir(os.path.join(htmdamberdir, f))
                and len(glob(os.path.join(htmdamberdir, f) + '/leaprc.*')) == 1]
    print('---- Extra forcefield files list: ' + path.join(htmdamberdir, '') + ' ----')
    for f in extraffs:
        print(f)

def build(mol, ff=None, topo=None, param=None, prefix='structure', outdir='./', caps=None, ionize=True, saltconc=0,
          saltanion=None, saltcation=None, disulfide=None, tleap='tleap', execute=True):
    """ Builds a system for AMBER

    Uses tleap to build a system for AMBER. Additionally it allows the user to ionize and add disulfide bridges.

    Parameters
    ----------
    mol : :class:`Molecule <htmd.molecule.molecule.Molecule>` object
        The Molecule object containing the system
    ff : list of str
        A list of leaprc forcefield files. Default: ['leaprc.lipid14', 'leaprc.ff14SB', 'leaprc.gaff']
    topo : list of str
        A list of topology `prepi` files.
    param : list of str
        A list of parameter `frcmod` files.
    prefix : str
        The prefix for the generated pdb and psf files
    outdir : str
        The path to the output directory
    caps : dict
        A dictionary with keys segids and values lists of strings describing the caps of that segment.
        e.g. caps['P'] = ['ACE', 'NME']. Default: will apply ACE and NME caps to proteins and no caps
        to the rest.
    ionize : bool
        Enable or disable ionization
    saltconc : float
        Salt concentration to add to the system after neutralization.
    saltanion : {'Cl-'}
        The anion type. Please use only AMBER ion atom names.
    saltcation : {'Na+', 'K+', 'Cs+'}
        The cation type. Please use only AMBER ion atom names.
    disulfide : np.ndarray
        If None it will guess disulfide bonds. Otherwise provide a 2D array where each row is a pair of atom indexes that makes a disulfide bond
    tleap : str
        Path to tleap executable used to build the system for AMBER
    execute : bool
        Disable building. Will only write out the input script needed by tleap. Does not include ionization.

    Returns
    -------
    molbuilt : :class:`Molecule <htmd.molecule.molecule.Molecule>` object
        The built system in a Molecule object

    Example
    -------
    >>> ffs = ['leaprc.lipid14', 'leaprc.ff14SB', 'leaprc.gaff']
    >>> molbuilt = amber.build(mol, ff=ffs, outdir='/tmp/build', saltconc=0.15)
    """
    # Remove pdb bonds!
    mol = mol.copy()
    mol.bonds = np.empty((0, 2), dtype=np.uint32)
    if shutil.which(tleap) is None:
        raise NameError('Could not find executable: `' + tleap + '` in the PATH. Cannot build for AMBER.')
    if not os.path.isdir(outdir):
        os.makedirs(outdir)
    _cleanOutDir(outdir)
    if ff is None:
        ff = ['leaprc.lipid14', 'leaprc.ff14SB', 'leaprc.gaff']
    if topo is None:
        topo = []
    if param is None:
        param = []
    if caps is None:
        caps = _defaultCaps(mol)

    _missingSegID(mol)
    _checkMixedSegment(mol)

    logger.info('Converting CHARMM membranes to AMBER.')
    mol = _charmmLipid2Amber(mol)

    #_checkProteinGaps(mol)
    _applyCaps(mol, caps)

    f = open(path.join(outdir, 'tleap.in'), 'w')
    f.write('# tleap file generated by amber.build\n')

    # Printing out the forcefields
    if isinstance(ff, str):
        ff = [ff]
    for force in ff:
        f.write('source ' + force + '\n')
    f.write('\n')

    # Loading TIP3P water parameters
    f.write('# Loading ions and TIP3P water parameters\n')
    f.write('loadamberparams frcmod.ionsjc_tip3p\n\n')

    # Loading user parameters
    f.write('# Loading parameter files\n')
    for p in param:
        shutil.copy(p, outdir)
        f.write('loadamberparams ' + path.basename(p) + '\n')
    f.write('\n')

    # Printing out topologies
    f.write('# Loading prepi topologies\n')
    for t in topo:
        shutil.copy(t, outdir)
        f.write('loadamberprep ' + path.basename(t) + '\n')
    f.write('\n')

    # Printing and loading the PDB file. AMBER can work with a single PDB file if the segments are separate by TER
    logger.info('Writing PDB file for input to tleap.')
    pdbname = path.join(outdir, 'input.pdb')
    mol.write(pdbname)
    if not os.path.isfile(pdbname):
        raise NameError('Could not write a PDB file out of the given Molecule.')
    f.write('# Loading the system\n')
    f.write('mol = loadpdb input.pdb\n\n')

    # Printing out patches for the disulfide bridges
    if disulfide is None and not ionize:
        logger.info('Detecting disulfide bonds.')
        disulfide = detectDisulfideBonds(mol)

    if not ionize and len(disulfide) != 0:  # Only make disu bonds after ionizing!
        f.write('# Adding disulfide bonds\n')
        for d in disulfide:
            # Convert to stupid amber residue numbering
            uqseqid = sequenceID((mol.resid, mol.insertion, mol.segid)) + mol.resid[0] - 1
            uqres1 = int(np.unique(uqseqid[mol.atomselect('segid {} and resid {}'.format(d.segid1, d.resid1))]))
            uqres2 = int(np.unique(uqseqid[mol.atomselect('segid {} and resid {}'.format(d.segid2, d.resid2))]))
            # Rename the CYS to CYX if there is a disulfide bond
            mol.set('resname', 'CYX', sel='segid {} and resid {}'.format(d.segid1, d.resid1))
            mol.set('resname', 'CYX', sel='segid {} and resid {}'.format(d.segid2, d.resid2))
            f.write('bond mol.{}.SG mol.{}.SG\n'.format(uqres1, uqres2))
        f.write('\n')

    f.write('# Writing out the results\n')
    f.write('saveamberparm mol ' + prefix + '.prmtop ' + prefix + '.crd\n')
    f.write('quit')
    f.close()

    molbuilt = None
    if execute:
        # Source paths of extra dirs
        htmdamberdir = path.join(home(), 'builder', 'amberfiles')
        sourcepaths = [htmdamberdir]
        sourcepaths += [path.join(htmdamberdir, path.dirname(f)) for f in ff]
        extrasource = ''
        for p in sourcepaths:
            extrasource += '-I {} '.format(p)
        logpath = os.path.abspath('{}/log.txt'.format(outdir))
        logger.info('Starting the build.')
        currdir = os.getcwd()
        os.chdir(outdir)
        f = open(logpath, 'w')
        try:
            call([tleap, extrasource, '-f', './tleap.in'], stdout=f)
        except:
            raise NameError('tleap failed at execution')
        f.close()
        os.chdir(currdir)
        logger.info('Finished building.')

        if path.getsize(path.join(outdir, 'structure.crd')) != 0 and path.getsize(path.join(outdir, 'structure.prmtop')) != 0:
            molbuilt = Molecule(path.join(outdir, 'structure.prmtop'))
            molbuilt.read(path.join(outdir, 'structure.crd'))
        else:
            raise NameError('No structure pdb/prmtop file was generated. Check {} for errors in building.'.format(logpath))

        if ionize:
            shutil.move(path.join(outdir, 'structure.crd'), path.join(outdir, 'structure.noions.crd'))
            shutil.move(path.join(outdir, 'structure.prmtop'), path.join(outdir, 'structure.noions.prmtop'))
            totalcharge = np.sum(molbuilt.charge)
            nwater = np.sum(molbuilt.atomselect('water and noh'))
            anion, cation, anionatom, cationatom, nanion, ncation = ionizef(totalcharge, nwater, saltconc=saltconc, ff='amber', anion=saltanion, cation=saltcation)
            newmol = ionizePlace(mol, anion, cation, anionatom, cationatom, nanion, ncation)
            # Redo the whole build but now with ions included
            return build(newmol, ff=ff, topo=topo, param=param, prefix=prefix, outdir=outdir, caps={}, ionize=False,
                         execute=execute, saltconc=saltconc, disulfide=disulfide, tleap=tleap)
    molbuilt.write(path.join(outdir, 'structure.pdb'))
    return molbuilt


def _applyCaps(mol, caps):
    for seg in caps:
        aceatm = mol.atomselect('segid {} and resname ACE'.format(seg))
        nmeatm = mol.atomselect('segid {} and resname NME'.format(seg))
        if np.sum(aceatm) != 0 and np.sum(nmeatm) != 0:
            logger.warning('ACE and NME caps detected on segid {}.'.format(seg))
            continue
        # This is the (horrible) way of adding caps in tleap:
        # 1. To add ACE remove two hydrogens bound to N eg:- H1,H3 then change the H2 atom to the ACE C atom
        # 2. In adding NME, remove the OXT oxygen and in that place, put the N atom of NME
        # 3. reoder to put the new atoms first and last
        # 4. Give them unique resids
        # Toni: XPLOR names for H[123] is HT[123];  OXT is OT1 . The following code assumes
        # just one of the two is present.
        segment = mol.atomselect('segid {}'.format(seg))
        segmentfirst = np.where(segment)[0][0]
        segmentlast = np.where(segment)[0][-1]
        resids = np.unique(mol.get('resid', sel=segment))
        ntermAtomToMod = mol.atomselect('segid {} and resid {} and name H2 HT2'.format(seg, np.min(resids)), indexes=True)
        ctermAtomToMod = mol.atomselect('segid {} and resid {} and name OXT OT1'.format(seg, np.max(resids)), indexes=True)
        if len(ntermAtomToMod) != 1:
            ntermAtomToMod = mol.atomselect('segid {} and resid {} and name H'.format(seg, np.min(resids)),
                                            indexes=True)
            if len(ntermAtomToMod) == 1:
                logger.info("Segid {}, resid {} does not have OXT or OT1, falling back at atom H".format(seg, np.min(resids)))
            else:
                raise AssertionError('Segment {}, resid {} should have an H2, an HT2, or an H atom. Cannot cap.'.format(seg, np.min(resids)))
        if len(ctermAtomToMod) != 1:
            ctermAtomToMod = mol.atomselect('segid {} and resid {} and name O'.format(seg, np.max(resids)),
                                            indexes=True)
            if len(ctermAtomToMod) == 1:
                logger.info("Segid {}, resid {} does not have OXT or OT1, falling back at atom O".format(seg, np.max(resids)))
            else:
                raise AssertionError('Segment {}, resid {} should have an OXT, an OT1, or an O atom. Cannot cap.'.format(seg, np.max(resids)))
        mol.set('resname', caps[seg][0], sel=ntermAtomToMod)
        mol.set('name', 'C', sel=ntermAtomToMod)
        mol.set('resid', np.min(resids)-1, sel=ntermAtomToMod)
        mol.set('resname', caps[seg][1], sel=ctermAtomToMod)
        mol.set('name', 'N', sel=ctermAtomToMod)
        mol.set('resid', np.max(resids)+1, sel=ctermAtomToMod)

        neworder = np.arange(mol.numAtoms)
        neworder[ntermAtomToMod] = segmentfirst
        neworder[segmentfirst] = ntermAtomToMod
        neworder[ctermAtomToMod] = segmentlast
        neworder[segmentlast] = ctermAtomToMod
        _reorderMol(mol, neworder)

        torem = mol.atomselect('segid {} and resid {} and name H1 H3 HT1 HT3'.format(seg, np.min(resids)))
        if np.sum(torem) != 2:
            logger.warning('Segment {}, resid {} should have H[123] or HT[123] atoms. Cannot cap. '
                                 'Capping in AMBER requires hydrogens on the residues that will be capped. '
                                 'Consider using the proteinPrepare function to add hydrogens to your molecule '
                                 'before building.'.format(seg, np.min(resids)))
        mol.remove(torem)


def _defaultCaps(mol):
    # neutral for protein, nothing for any other segment
    # of course this might not be ideal for protein which require charged terminals

    segsProt = np.unique(mol.get('segid', sel='protein'))
    caps = dict()
    for s in segsProt:
        caps[s] = ['ACE', 'NME']
    return caps


def _cleanOutDir(outdir):
    from glob import glob
    files = glob(os.path.join(outdir, 'structure.*'))
    files += glob(os.path.join(outdir, 'log.*'))
    files += glob(os.path.join(outdir, '*.log'))
    for f in files:
        os.remove(f)


def _charmmLipid2Amber(mol):
    """ Convert a CHARMM lipid membrane to AMBER format

    Parameters
    ----------
    mol : :class:`Molecule <htmd.molecule.molecule.Molecule>` object
        The Molecule object containing the membrane

    Returns
    -------
    newmol : :class:`Molecule <htmd.molecule.molecule.Molecule>` object
        A new Molecule object with the membrane converted to AMBER
    """
    resdict = _readcsvdict(path.join(home(), 'builder', 'charmmlipid2amber.csv'))

    natoms = mol.numAtoms
    neworder = np.array(list(range(natoms)))  # After renaming the atoms and residues I have to reorder them

    begs = np.zeros(natoms, dtype=bool)
    fins = np.zeros(natoms, dtype=bool)
    begters = np.zeros(natoms, dtype=bool)
    finters = np.zeros(natoms, dtype=bool)

    betabackup = mol.beta.copy()

    mol = mol.copy()
    mol.set('beta', sequenceID(mol.resid))
    for res in resdict.keys():
        molresidx = mol.resname == res
        if not np.any(molresidx):
            continue
        names = mol.name.copy()  # Need to make a copy or I accidentally double-modify atoms

        atommap = resdict[res]
        for atom in atommap.keys():
            rule = atommap[atom]

            molatomidx = np.zeros(len(names), dtype=bool)
            molatomidx[molresidx] = names[molresidx] == atom

            mol.set('resname', rule.replaceresname, sel=molatomidx)
            mol.set('name', rule.replaceatom, sel=molatomidx)
            neworder[molatomidx] = rule.order

            if rule.order == 0:  # First atom (with or without ters)
                begs[molatomidx] = True
            if rule.order == rule.natoms - 1:  # Last atom (with or without ters)
                fins[molatomidx] = True
            if rule.order == 0 and rule.ter:  # First atom with ter
                begters[molatomidx] = True
            if rule.order == rule.natoms - 1 and rule.ter:  # Last atom with ter
                finters[molatomidx] = True

    betas = np.unique(mol.beta[begs])
    residuebegs = np.ones(len(betas), dtype=int) * -1
    residuefins = np.ones(len(betas), dtype=int) * -1
    for i in range(len(betas)):
        residuebegs[i] = np.where(mol.beta == betas[i])[0][0]
        residuefins[i] = np.where(mol.beta == betas[i])[0][-1]
    for i in range(len(residuebegs)):
        beg = residuebegs[i]
        fin = residuefins[i] + 1
        neworder[beg:fin] = neworder[beg:fin] + beg
    idx = np.argsort(neworder)
    mol.beta = betabackup
    _reorderMol(mol, idx)

    begters = np.where(begters[idx])[0]  # Sort the begs and ters
    finters = np.where(finters[idx])[0]

    if len(begters) > 999:
        raise NameError('More than 999 lipids. Cannot define separate segments for all of them.')

    for i in range(len(begters)):
        map = np.zeros(len(mol.resid), dtype=bool)
        map[begters[i]:finters[i]+1] = True
        mol.set('resid', sequenceID(mol.get('resname', sel=map)), sel=map)
        mol.set('segid', 'L' + str(i+1), sel=map)

    return mol


def _reorderMol(mol, order):
    for k in mol._append_fields:
        if mol.__dict__[k] is not None and np.size(mol.__dict__[k]) != 0:
            if k == 'coords':
                mol.__dict__[k] = mol.__dict__[k][order, :, :]
            else:
                mol.__dict__[k] = mol.__dict__[k][order]


def _readcsvdict(filename):
    import csv
    from collections import namedtuple
    if os.path.isfile(filename):
        csvfile = open(filename, 'r')
    else:
        raise NameError('File ' + filename + ' does not exist')

    resdict = dict()

    Rule = namedtuple('Rule', ['replaceresname', 'replaceatom', 'order', 'natoms', 'ter'])

    # Skip header line of csv file. Line 2 contains dictionary keys:
    csvfile.readline()
    csvreader = csv.DictReader(csvfile)
    for line in csvreader:
        searchres = line['search'].split()[1]
        searchatm = line['search'].split()[0]
        if searchres not in resdict:
            resdict[searchres] = dict()
        resdict[searchres][searchatm] = Rule(line['replace'].split()[1], line['replace'].split()[0], int(line['order']), int(line['num_atom']), line['TER'] == 'True')
    csvfile.close()

    return resdict


if __name__ == '__main__':
    from htmd.molecule.molecule import Molecule
    from htmd.builder.solvate import solvate
    from htmd.builder.preparation import proteinPrepare
    from htmd.home import home
    from htmd.util import tempname
    import os
    from glob import glob
    import numpy as np
    from htmd.util import diffMolecules

    np.random.seed(1)
    mol = Molecule('3PTB')
    mol.filter('protein')
    mol = proteinPrepare(mol)
    smol = solvate(mol)
    ffs = ['leaprc.lipid14', 'leaprc.ff14SB', 'leaprc.gaff']
    tmpdir = tempname()
    bmol = build(smol, ff=ffs, outdir=tmpdir)

    compare = home(dataDir=os.path.join('test-amber-build', '3PTB'))
    mol = Molecule(os.path.join(compare, 'structure.prmtop'))

    assert np.array_equal(mol.bonds, bmol.bonds)

    assert len(diffMolecules(mol, bmol)) == 0
