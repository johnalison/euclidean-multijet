# conda create -n coffea_torch coffea pytorch
# conda activate coffea_torch

import time
import awkward as ak
import numpy as np
from functools import partial

import hist # https://hist.readthedocs.io/en/latest/index.html

# https://coffeateam.github.io/coffea
from coffea.nanoevents import NanoEventsFactory, NanoAODSchema, BaseSchema
from coffea import processor, util
from coffea.nanoevents.methods import vector
ak.behavior.update(vector.behavior)

NanoAODSchema.warn_missing_crossrefs = False
import warnings
warnings.filterwarnings("ignore")


# get the number of non-dict objects in a nested dictionary
def count_nested_dict(nested_dict, c=0):
    for key in nested_dict:
        if isinstance(nested_dict[key], dict):
            c = count_nested_dict(nested_dict[key], c)
        else:
            c += 1
    return c


class analysis(processor.ProcessorABC):
    def __init__(self):
        self.debug = False
    
    def process(self, event):
        tstart = time.time()
        np.random.seed(0)
        
        fname   = event.metadata['filename']
        dataset = event.metadata['dataset']
        estart  = event.metadata['entrystart']
        estop   = event.metadata['entrystop']
        chunk   = f'{dataset}::{estart:6d}:{estop:6d} >>> '

        dataset_axis = hist.axis.StrCategory([], growth=True, name='dataset', label='Dataset')
        cut_axis = hist.axis.StrCategory([], growth=True, name='cut', label='Cut')
        region_axis = hist.axis.StrCategory([], growth=True, name='region', label='Region')
        mass_axis = hist.axis.Regular(300, 0, 1500, name='mass', label=r'$m_{4j}$ [GeV]')

        output = {'hists': {},
                  'cutflow': hist.Hist(dataset_axis, cut_axis, region_axis, storage='weight', label='Events'),
                  'sumw': ak.sum(event.weight),
                  'nEvent': len(event)}


        output['hists']['m4j'] = hist.Hist(dataset_axis,
                                           cut_axis,
                                           region_axis,
                                           mass_axis,
                                           storage='weight', label='Events')

        # compute four-vector of sum of jets, for the toy samples there are always four jets
        v4j = ak.sum(event.Jet, axis=1)
        event['v4j'] = v4j

        output['cutflow'].fill(dataset=dataset, cut='all', region=['inclusive']*len(event), weight=event.weight) # bug in boost_histogram, waiting for fix, https://github.com/scikit-hep/boost-histogram/issues/452
        output['hists']['m4j'].fill(dataset=dataset, cut='all', region='inclusive', mass=event.v4j.mass, weight=event.weight)

        # Jet selection
        event['Jet', 'selected'] = (event.Jet.pt>=40) & (np.abs(event.Jet.eta)<=2.4)
        event['nJet_selected'] = ak.sum(event.Jet.selected, axis=1)
        event = event[event.nJet_selected>=4]

        output['cutflow'].fill(dataset=dataset, cut='preselection', region=['inclusive']*len(event), weight=event.weight)
        output['hists']['m4j'].fill(dataset=dataset, cut='preselection', region='inclusive', mass=event.v4j.mass, weight=event.weight)

        #
        # Build diJets, indexed by diJet[event,pairing,0/1]
        #
        pairing = [([0,2],[0,1],[0,1]),
                   ([1,3],[2,3],[3,2])]
        diJet         = event.Jet[:,pairing[0]]     +   event.Jet[:,pairing[1]]
        diJet['st']   = event.Jet[:,pairing[0]].pt  +   event.Jet[:,pairing[1]].pt
        diJet['dr']   = event.Jet[:,pairing[0]].delta_r(event.Jet[:,pairing[1]])
        diJet['lead'] = event.Jet[:,pairing[0]]
        diJet['subl'] = event.Jet[:,pairing[1]]
        # Sort diJets within pairings to be lead st, subl st
        diJet = diJet[ak.argsort(diJet.st, axis=2, ascending=False)]
        # Now indexed by diJet[event,pairing,lead/subl st]

        # Compute diJetMass cut with independent min/max for lead/subl
        minDiJetMass = np.array([[[ 52, 50]]])
        maxDiJetMass = np.array([[[180,173]]])
        diJet['diJetMass'] = (minDiJetMass < diJet.mass) & (diJet.mass < maxDiJetMass)

        # Compute sliding window delta_r criteria (drc)
        min_m4j_scale = np.array([[ 360, 235]])
        min_dr_offset = np.array([[-0.5, 0.0]])
        max_m4j_scale = np.array([[ 650, 650]])
        max_dr_offset = np.array([[ 0.5, 0.7]])
        max_dr        = np.array([[ 1.5, 1.5]])
        m4j = np.repeat(np.reshape(np.array(event.v4j.mass), (-1,1,1)), 2, axis=2)
        diJet['drc'] = (min_m4j_scale/m4j + min_dr_offset < diJet.dr) & (diJet.dr < np.maximum(max_m4j_scale/m4j + max_dr_offset, max_dr))

        # Compute consistency of diJet masses with higgs boson mass
        mH = 125.0
        st_bias = np.array([[[1.02, 0.98]]])
        cH = mH * st_bias
        diJet['xH'] = (diJet.mass - cH)/(0.1*diJet.mass)

        #
        # Build quadJets
        #
        quadJet = ak.zip({'lead': diJet[:,:,0],
                          'subl': diJet[:,:,1],
                          'diJetMass': ak.all(diJet.diJetMass, axis=2),
                          'random': np.random.uniform(low=0.1, high=0.9, size=(diJet.__len__(), 3))
                          })#, with_name='quadJet')
        quadJet['dr'] = quadJet['lead'].delta_r(quadJet['subl'])
        # Compute Region
        quadJet['xHH'] = np.sqrt(quadJet.lead.xH**2 + quadJet.subl.xH**2)
        max_xHH = 1.9
        quadJet['SR'] = quadJet.xHH < max_xHH
        quadJet['SB'] = quadJet.diJetMass & ~quadJet.SR
        
        # pick quadJet at random giving preference to ones which pass diJetMass and drc's
        quadJet['rank'] = 10*quadJet.lead.diJetMass + 10*quadJet.subl.diJetMass + quadJet.lead.drc + quadJet.subl.drc + quadJet.random
        quadJet['selected'] = quadJet.rank == np.max(quadJet.rank, axis=1)

        event[  'diJet'] =   diJet
        event['quadJet'] = quadJet
        event['quadJet_selected'] = quadJet[quadJet.selected][:,0]
        event['diJetMass'] = event.quadJet_selected.diJetMass
        event['SB'] = event.quadJet_selected.SB
        event['SR'] = event.quadJet_selected.SR
        
        self.fill(output, event[event.diJetMass], dataset=dataset, cut='preselection', region='diJetMass')
        self.fill(output, event[event.SB], dataset=dataset, cut='preselection', region='SB')
        self.fill(output, event[event.SR], dataset=dataset, cut='preselection', region='SR')
                
        # Done
        elapsed = time.time() - tstart
        if self.debug: print(f'{chunk}{nEvent/elapsed:,.0f} events/s')
        return output

    def fill(self, output, event, dataset='', cut='', region=''):
        output['cutflow'].fill(dataset=dataset, cut=cut, region=[region]*len(event), weight=event.weight)
        output['hists']['m4j'].fill(dataset=dataset, cut=cut, region=region, mass=event.v4j.mass, weight=event.weight)

    def postprocess(self, accumulator):
        pass


if __name__ == '__main__':
    datasets  = []
    datasets += ['data/fourTag_picoAOD.root']
    datasets += ['data/threeTag_picoAOD.root']

    fileset = {}
    for dataset in datasets:
        fileset[dataset] = {'files': [dataset],
                            'metadata': {}}
    print(fileset)
    tstart = time.time()
    output = processor.run_uproot_job(fileset,
                                      treename='Events',
                                      processor_instance=analysis(),
                                      executor=processor.futures_executor,
                                      executor_args={'schema': NanoAODSchema, 'workers': 4},
                                      chunksize=100_000,
                                      #maxchunks=1,
                                      )
    elapsed = time.time() - tstart
    nEvent = output['nEvent'] #sum([output['nEvent'][dataset] for dataset in output['nEvent'].keys()])
    nHists = count_nested_dict(output['hists'])
    print('nHists',nHists)
    print(f'{nEvent/elapsed:,.0f} events/s ({nEvent:,}/{elapsed:,.2f})')
