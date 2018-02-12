# -*- coding: utf-8 -*-
"""
A python module to run surface wave eikonal/Helmholtz tomography
The code creates a datadbase based on hdf5 data format

:Dependencies:
    pyasdf and its dependencies
    GMT 5.x.x (for interpolation on Earth surface)
    numba
    numexpr
    
:Copyright:
    Author: Lili Feng
    Graduate Research Assistant
    CIEI, Department of Physics, University of Colorado Boulder
    email: lili.feng@colorado.edu
    
:References:
    Lin, Fan-Chi, Michael H. Ritzwoller, and Roel Snieder. "Eikonal tomography: surface wave tomography by phase front tracking across a regional broad-band seismic array."
        Geophysical Journal International 177.3 (2009): 1091-1110.
    Lin, Fan-Chi, and Michael H. Ritzwoller. "Helmholtz surface wave tomography for isotropic and azimuthally anisotropic structure."
        Geophysical Journal International 186.3 (2011): 1104-1120.
"""
import numpy as np
import numpy.ma as ma
import h5py, pyasdf
import os, shutil
from subprocess import call
from mpl_toolkits.basemap import Basemap, shiftgrid, cm
import matplotlib.pyplot as plt
from matplotlib.mlab import griddata
import colormaps
import obspy
import field2d_earth
import numexpr
import warnings
from functools import partial
import multiprocessing
from numba import jit, float32, int32

# compiled function to get weight for each event and each grid point
@jit(float32[:,:,:](float32[:,:,:], float32[:,:,:]))
def _get_azi_weight(aziALL, validALL):
    Nevent, Nlon, Nlat  = aziALL.shape
    weightALL           = np.zeros((Nevent, Nlon, Nlat), dtype=np.float32)
    for ilon in xrange(Nlon):
        for ilat in xrange(Nlat):
            for i in xrange(Nevent):
                for j in xrange(Nevent):
                    delAzi                      = abs(aziALL[i, ilon, ilat] - aziALL[j, ilon, ilat])
                    if delAzi < 20. or delAzi > 340.:
                        weightALL[i, ilon, ilat]+= validALL[i, ilon, ilat]    
    return weightALL

class EikonalTomoDataSet(h5py.File):
    
    def set_input_parameters(self, minlon, maxlon, minlat, maxlat, pers=np.array([]), dlon=0.2, dlat=0.2, \
                             nlat_grad=1, nlon_grad=1, nlat_lplc=2, nlon_lplc=2):
        """
        Set input parameters for tomographic inversion.
        =================================================================================================================
        ::: input parameters :::
        minlon, maxlon  - minimum/maximum longitude
        minlat, maxlat  - minimum/maximum latitude
        pers            - period array, default = np.append( np.arange(18.)*2.+6., np.arange(4.)*5.+45.)
        dlon, dlat      - longitude/latitude interval
        =================================================================================================================
        """
        if pers.size==0:
            # pers=np.arange(13.)*2.+6.
            pers    = np.append( np.arange(18.)*2.+6., np.arange(4.)*5.+45.)
        self.attrs.create(name = 'period_array', data=pers, dtype='f')
        self.attrs.create(name = 'minlon', data=minlon, dtype='f')
        self.attrs.create(name = 'maxlon', data=maxlon, dtype='f')
        self.attrs.create(name = 'minlat', data=minlat, dtype='f')
        self.attrs.create(name = 'maxlat', data=maxlat, dtype='f')
        self.attrs.create(name = 'dlon', data=dlon)
        self.attrs.create(name = 'dlat', data=dlat)
        Nlon        = int((maxlon-minlon)/dlon+1)
        Nlat        = int((maxlat-minlat)/dlat+1)
        self.attrs.create(name = 'Nlon', data=Nlon)
        self.attrs.create(name = 'Nlat', data=Nlat)
        self.attrs.create(name = 'nlat_grad', data=nlat_grad)
        self.attrs.create(name = 'nlon_grad', data=nlon_grad)
        self.attrs.create(name = 'nlat_lplc', data=nlat_lplc)
        self.attrs.create(name = 'nlon_lplc', data=nlon_lplc)
        return
    
    def xcorr_eikonal(self, inasdffname, workingdir, fieldtype='Tph', channel='ZZ', data_type='FieldDISPpmf2interp', runid=0, deletetxt=True, verbose=False):
        """
        Compute gradient of travel time for cross-correlation data
        =================================================================================================================
        ::: input parameters :::
        inasdffname - input ASDF data file
        workingdir  - working directory
        fieldtype   - fieldtype (Tph or Tgr)
        channel     - channel for analysis
        data_type   - data type
                     (default='FieldDISPpmf2interp', aftan measurements with phase-matched filtering and jump correction)
        runid       - run id
        deletetxt   - delete output txt files in working directory
        =================================================================================================================
        """
        if fieldtype!='Tph' and fieldtype!='Tgr':
            raise ValueError('Wrong field type: '+fieldtype+' !')
        create_group        = False
        while (not create_group):
            try:
                group       = self.create_group( name = 'Eikonal_run_'+str(runid) )
                create_group= True
            except:
                runid       += 1
                continue
        group.attrs.create(name = 'fieldtype', data=fieldtype[1:])
        inDbase             = pyasdf.ASDFDataSet(inasdffname)
        pers                = self.attrs['period_array']
        minlon              = self.attrs['minlon']
        maxlon              = self.attrs['maxlon']
        minlat              = self.attrs['minlat']
        maxlat              = self.attrs['maxlat']
        dlon                = self.attrs['dlon']
        dlat                = self.attrs['dlat']
        nlat_grad           = self.attrs['nlat_grad']
        nlon_grad           = self.attrs['nlon_grad']
        nlat_lplc           = self.attrs['nlat_lplc']
        nlon_lplc           = self.attrs['nlon_lplc']
        fdict               = { 'Tph': 2, 'Tgr': 3}
        evLst               = inDbase.waveforms.list()
        for per in pers:
            print 'Computing gradient for: '+str(per)+' sec'
            del_per         = per-int(per)
            if del_per==0.:
                persfx      = str(int(per))+'sec'
            else:
                dper        = str(del_per)
                persfx      = str(int(per))+'sec'+dper.split('.')[1]
            working_per     = workingdir+'/'+str(per)+'sec'
            per_group       = group.create_group( name='%g_sec'%( per ) )
            for evid in evLst:
                netcode1, stacode1  = evid.split('.')
                try:
                    subdset         = inDbase.auxiliary_data[data_type][netcode1][stacode1][channel][persfx]
                except KeyError:
                    print ('No travel time field for: '+evid)
                    continue
                if verbose:
                    print ('Event: '+evid)
                lat1, elv1, lon1    = inDbase.waveforms[evid].coordinates.values()
                if lon1<0.:
                    lon1            += 360.
                dataArr             = subdset.data.value
                field2d             = field2d_earth.Field2d(minlon=minlon, maxlon=maxlon, dlon=dlon,
                                        minlat=minlat, maxlat=maxlat, dlat=dlat, period=per, evlo=lon1, evla=lat1, fieldtype=fieldtype, \
                                        nlat_grad=nlat_grad, nlon_grad=nlon_grad, nlat_lplc=nlat_lplc, nlon_lplc=nlon_lplc)
                Zarr                = dataArr[:, fdict[fieldtype]]
                distArr             = dataArr[:, 5]
                field2d.read_array(lonArr=np.append(lon1, dataArr[:,0]), latArr=np.append(lat1, dataArr[:,1]), ZarrIn=np.append(0., distArr/Zarr) )
                outfname            = evid+'_'+fieldtype+'_'+channel+'.lst'
                field2d.interp_surface(workingdir=working_per, outfname=outfname)
                field2d.check_curvature(workingdir=working_per, outpfx=evid+'_'+channel+'_')
                field2d.gradient_qc(workingdir=working_per, inpfx=evid+'_'+channel+'_', nearneighbor=True, cdist=None)
                # save data to hdf5 dataset
                event_group         = per_group.create_group(name=evid)
                event_group.attrs.create(name = 'evlo', data=lon1)
                event_group.attrs.create(name = 'evla', data=lat1)
                appVdset            = event_group.create_dataset(name='appV', data=field2d.appV)
                reason_ndset        = event_group.create_dataset(name='reason_n', data=field2d.reason_n)
                proAngledset        = event_group.create_dataset(name='proAngle', data=field2d.proAngle)
                azdset              = event_group.create_dataset(name='az', data=field2d.az)
                bazdset             = event_group.create_dataset(name='baz', data=field2d.baz)
                Tdset               = event_group.create_dataset(name='travelT', data=field2d.Zarr)
        if deletetxt:
            shutil.rmtree(workingdir)
        return
    
    def xcorr_eikonal_mp(self, inasdffname, workingdir, fieldtype='Tph', channel='ZZ', data_type='FieldDISPpmf2interp', runid=0,
                deletetxt=True, verbose=True, subsize=1000, nprocess=None):
        """
        Compute gradient of travel time for cross-correlation data with multiprocessing
        =================================================================================================================
        ::: input parameters :::
        inasdffname - input ASDF data file
        workingdir  - working directory
        fieldtype   - fieldtype (Tph or Tgr)
        channel     - channel for analysis
        data_type   - data type
                     (default='FieldDISPpmf2interp', aftan measurements with phase-matched filtering and jump correction)
        runid       - run id
        deletetxt   - delete output txt files in working directory
        subsize     - subsize of processing list, use to prevent lock in multiprocessing process
        nprocess    - number of processes
        =================================================================================================================
        """
        if fieldtype!='Tph' and fieldtype!='Tgr':
            raise ValueError('Wrong field type: '+fieldtype+' !')
        create_group        = False
        while (not create_group):
            try:
                group       = self.create_group( name = 'Eikonal_run_'+str(runid) )
                create_group= True
            except:
                runid       += 1
                continue
        group.attrs.create(name = 'fieldtype', data=fieldtype[1:])
        inDbase             = pyasdf.ASDFDataSet(inasdffname)
        pers                = self.attrs['period_array']
        minlon              = self.attrs['minlon']
        maxlon              = self.attrs['maxlon']
        minlat              = self.attrs['minlat']
        maxlat              = self.attrs['maxlat']
        dlon                = self.attrs['dlon']
        dlat                = self.attrs['dlat']
        nlat_grad           = self.attrs['nlat_grad']
        nlon_grad           = self.attrs['nlon_grad']
        nlat_lplc           = self.attrs['nlat_lplc']
        nlon_lplc           = self.attrs['nlon_lplc']
        fdict               = { 'Tph': 2, 'Tgr': 3}
        evLst               = inDbase.waveforms.list()
        fieldLst            = []
        #------------------------
        # prepare data
        #------------------------
        for per in pers:
            print 'Preparing data for gradient computation of '+str(per)+' sec'
            del_per         = per-int(per)
            if del_per==0.:
                persfx      = str(int(per))+'sec'
            else:
                dper        = str(del_per)
                persfx      = str(int(per))+'sec'+dper.split('.')[1]
            working_per     = workingdir+'/'+str(per)+'sec'
            if not os.path.isdir(working_per):
                os.makedirs(working_per)
            for evid in evLst:
                netcode1, stacode1  = evid.split('.')
                try:
                    subdset         = inDbase.auxiliary_data[data_type][netcode1][stacode1][channel][persfx]
                except KeyError:
                    print 'No travel time field for: '+evid
                    continue
                lat1, elv1, lon1    = inDbase.waveforms[evid].coordinates.values()
                if lon1<0.:
                    lon1            += 360.
                dataArr             = subdset.data.value
                field2d             = field2d_earth.Field2d(minlon=minlon, maxlon=maxlon, dlon=dlon, minlat=minlat, maxlat=maxlat, dlat=dlat,
                                        period=per, evlo=lon1, evla=lat1, fieldtype=fieldtype, evid=evid, \
                                               nlat_grad=nlat_grad, nlon_grad=nlon_grad, nlat_lplc=nlat_lplc, nlon_lplc=nlon_lplc)
                Zarr                = dataArr[:, fdict[fieldtype]]
                distArr             = dataArr[:, 5]
                field2d.read_array(lonArr=np.append(lon1, dataArr[:,0]), latArr=np.append(lat1, dataArr[:,1]), ZarrIn=np.append(0., distArr/Zarr) )
                fieldLst.append(field2d)
        #-----------------------------------------
        # Computing gradient with multiprocessing
        #-----------------------------------------
        if len(fieldLst) > subsize:
            Nsub                    = int(len(fieldLst)/subsize)
            for isub in range(Nsub):
                print 'Subset:', isub,'in',Nsub,'sets'
                cfieldLst           = fieldLst[isub*subsize:(isub+1)*subsize]
                EIKONAL             = partial(eikonal4mp, workingdir=workingdir, channel=channel)
                pool                = multiprocessing.Pool(processes=nprocess)
                pool.map(EIKONAL, cfieldLst) #make our results with a map call
                pool.close() #we are not adding any more processes
                pool.join() #tell it to wait until all threads are done before going on
            cfieldLst               = fieldLst[(isub+1)*subsize:]
            EIKONAL                 = partial(eikonal4mp, workingdir=workingdir, channel=channel)
            pool                    = multiprocessing.Pool(processes=nprocess)
            pool.map(EIKONAL, cfieldLst) #make our results with a map call
            pool.close() #we are not adding any more processes
            pool.join() #tell it to wait until all threads are done before going on
        else:
            EIKONAL                 = partial(eikonal4mp, workingdir=workingdir, channel=channel)
            pool                    = multiprocessing.Pool(processes=nprocess)
            pool.map(EIKONAL, fieldLst) #make our results with a map call
            pool.close() #we are not adding any more processes
            pool.join() #tell it to wait until all threads are done before going on
        #-----------------------------------------
        # Read data into hdf5 dataset
        #-----------------------------------------
        for per in pers:
            print 'Reading gradient data for: '+str(per)+' sec'
            working_per = workingdir+'/'+str(per)+'sec'
            per_group   = group.create_group( name='%g_sec'%( per ) )
            for evid in evLst:
                infname = working_per+'/'+evid+'_field2d.npz'
                if not os.path.isfile(infname):
                    print 'No data for:', evid; continue
                InArr           = np.load(infname)
                appV            = InArr['arr_0']
                reason_n        = InArr['arr_1']
                proAngle        = InArr['arr_2']
                az              = InArr['arr_3']
                baz             = InArr['arr_4']
                Zarr            = InArr['arr_5']
                lat1, elv1, lon1= inDbase.waveforms[evid].coordinates.values()
                # save data to hdf5 dataset
                event_group     = per_group.create_group(name=evid)
                event_group.attrs.create(name = 'evlo', data=lon1)
                event_group.attrs.create(name = 'evla', data=lat1)
                appVdset        = event_group.create_dataset(name='appV', data=appV)
                reason_ndset    = event_group.create_dataset(name='reason_n', data=reason_n)
                proAngledset    = event_group.create_dataset(name='proAngle', data=proAngle)
                azdset          = event_group.create_dataset(name='az', data=az)
                bazdset         = event_group.create_dataset(name='baz', data=baz)
                Tdset           = event_group.create_dataset(name='travelT', data=Zarr)
        if deletetxt:
            shutil.rmtree(workingdir)
        return
    
    def quake_eikonal(self, inasdffname, workingdir, fieldtype='Tph', channel='Z', data_type='FieldDISPpmf2interp',
            runid=0, merge=False, deletetxt=False, verbose=True, amplplc=False):
        """
        Compute gradient of travel time for earthquake data
        =================================================================================================================
        ::: input parameters :::
        inasdffname - input ASDF data file
        workingdir  - working directory
        fieldtype   - fieldtype (Tph or Tgr)
        channel     - channel for analysis
        data_type   - data type
                     (default='FieldDISPpmf2interp', aftan measurements with phase-matched filtering and jump correction)
        runid       - run id
        deletetxt   - delete output txt files in working directory
        amplplc     - compute amplitude Laplacian term or not
        =================================================================================================================
        """
        if fieldtype!='Tph' and fieldtype!='Tgr':
            raise ValueError('Wrong field type: '+fieldtype+' !')
        if merge:
            try:
                group=self.create_group( name = 'Eikonal_run_'+str(runid) )
                group.attrs.create(name = 'fieldtype', data=fieldtype[1:])
            except ValueError:
                print 'Merging Eikonal run id: ',runid
                pass
        else:
            create_group=False
            while (not create_group):
                try:
                    group=self.create_group( name = 'Eikonal_run_'+str(runid) )
                    create_group=True
                except:
                    runid+=1
                    continue
            group.attrs.create(name = 'fieldtype', data=fieldtype[1:])
        inDbase=pyasdf.ASDFDataSet(inasdffname)
        pers = self.attrs['period_array']
        minlon=self.attrs['minlon']
        maxlon=self.attrs['maxlon']
        minlat=self.attrs['minlat']
        maxlat=self.attrs['maxlat']
        dlon=self.attrs['dlon']
        dlat=self.attrs['dlat']
        fdict={ 'Tph': 2, 'Tgr': 3, 'Amp': 4}
        evLst=inDbase.events
        for per in pers:
            print 'Computing gradient for: '+str(per)+' sec'
            del_per=per-int(per)
            if del_per==0.:
                persfx=str(int(per))+'sec'
            else:
                dper=str(del_per)
                persfx=str(int(per))+'sec'+dper.split('.')[1]
            working_per=workingdir+'/'+str(per)+'sec'
            per_group=group.require_group( name='%g_sec'%( per ) )
            evnumb=0
            for event in evLst:
                evnumb+=1
                evid='E%05d' % evnumb
                try:
                    subdset = inDbase.auxiliary_data[data_type][evid+'_'+channel][persfx]
                except KeyError:
                    print 'No travel time field for: '+evid
                    continue
                magnitude=event.magnitudes[0].mag; Mtype=event.magnitudes[0].magnitude_type
                event_descrip=event.event_descriptions[0].text+', '+event.event_descriptions[0].type
                evlo=event.origins[0].longitude; evla=event.origins[0].latitude
                if verbose: print 'Event: '+event_descrip+', '+Mtype+' = '+str(magnitude) 
                if evlo<0.: evlo+=360.
                dataArr = subdset.data.value
                field2d=field2d_earth.Field2d(minlon=minlon, maxlon=maxlon, dlon=dlon,
                        minlat=minlat, maxlat=maxlat, dlat=dlat, period=per, evlo=evlo, evla=evla, fieldtype=fieldtype)
                Zarr=dataArr[:, fdict[fieldtype]]
                distArr=dataArr[:, 6] # Note amplitude in added!!!
                field2d.read_array(lonArr=np.append(evlo, dataArr[:,0]), latArr=np.append(evla, dataArr[:,1]), ZarrIn=np.append(0., distArr/Zarr) )
                outfname=evid+'_'+fieldtype+'_'+channel+'.lst'
                field2d.interp_surface(workingdir=working_per, outfname=outfname)
                field2d.check_curvature(workingdir=working_per, outpfx=evid+'_'+channel+'_')
                field2d.gradient_qc(workingdir=working_per, inpfx=evid+'_'+channel+'_', nearneighbor=True, cdist=None)
                # save data to hdf5 dataset
                event_group=per_group.create_group(name=evid)
                event_group.attrs.create(name = 'evlo', data=evlo)
                event_group.attrs.create(name = 'evla', data=evla)
                appVdset     = event_group.create_dataset(name='appV', data=field2d.appV)
                reason_ndset = event_group.create_dataset(name='reason_n', data=field2d.reason_n)
                proAngledset = event_group.create_dataset(name='proAngle', data=field2d.proAngle)
                azdset       = event_group.create_dataset(name='az', data=field2d.az)
                bazdset      = event_group.create_dataset(name='baz', data=field2d.baz)
                Tdset        = event_group.create_dataset(name='travelT', data=field2d.Zarr)
                if amplplc:
                    field2dAmp=field2d_earth.Field2d(minlon=minlon, maxlon=maxlon, dlon=dlon,
                        minlat=minlat, maxlat=maxlat, dlat=dlat, period=per, evlo=evlo, evla=evla, fieldtype='Amp')
                    field2dAmp.read_array(lonArr=dataArr[:,0], latArr=dataArr[:,1], ZarrIn=dataArr[:, fdict['Amp']] )
                    outfnameAmp=evid+'_Amp_'+channel+'.lst'
                    field2dAmp.interp_surface(workingdir=working_per, outfname=outfnameAmp)
                    field2dAmp.gradient()
                    field2dAmp.cut_edge(1,1)
                    field2dAmp.Laplacian()
                    field2dAmp.cut_edge(1,1)
                    field2dAmp.get_lplc_amp()
                    lplc_ampdset = event_group.create_dataset(name='lplc_amp', data=field2dAmp.lplc_amp)
                    field2dAmp.lplc_amp[field2dAmp.lplc_amp > 2e-2]=0
                    field2dAmp.lplc_amp[field2dAmp.lplc_amp < -2e-2]=0
                    slownessApp=-np.ones(field2d.appV.shape)
                    slownessApp[field2d.appV!=0]=1./field2d.appV[field2d.appV!=0]
                    temp=slownessApp**2-field2dAmp.lplc_amp
                    temp[temp<0]=0
                    slownessCor=np.sqrt(temp)
                    corV=np.zeros(slownessCor.shape)
                    corV[slownessCor!=0]=1./slownessCor[slownessCor!=0]
                    corV_ampdset = event_group.create_dataset(name='corV', data=corV)
                # field2d.appV=corV
                return field2d
        if deletetxt: shutil.rmtree(workingdir)
        return
    
    def quake_eikonal_mp(self, inasdffname, workingdir, fieldtype='Tph', channel='Z', data_type='FieldDISPpmf2interp', runid=0,
                merge=False, deletetxt=True, verbose=True, subsize=1000, nprocess=None, amplplc=False):
        """
        Compute gradient of travel time for cross-correlation data with multiprocessing
        =================================================================================================================
        ::: input parameters :::
        inasdffname - input ASDF data file
        workingdir  - working directory
        fieldtype   - fieldtype (Tph or Tgr)
        channel     - channel for analysis
        data_type   - data type
                     (default='FieldDISPpmf2interp', aftan measurements with phase-matched filtering and jump correction)
        runid       - run id
        deletetxt   - delete output txt files in working directory
        subsize     - subsize of processing list, use to prevent lock in multiprocessing process
        nprocess    - number of processes
        amplplc     - compute amplitude Laplacian term or not
        =================================================================================================================
        """
        if fieldtype!='Tph' and fieldtype!='Tgr':
            raise ValueError('Wrong field type: '+fieldtype+' !')
        if merge:
            try:
                group=self.create_group( name = 'Eikonal_run_'+str(runid) )
                group.attrs.create(name = 'fieldtype', data=fieldtype[1:])
            except ValueError:
                print 'Merging Eikonal run id: ',runid
                pass
        else:
            create_group=False
            while (not create_group):
                try:
                    group=self.create_group( name = 'Eikonal_run_'+str(runid) )
                    create_group=True
                except:
                    runid+=1
                    continue
            group.attrs.create(name = 'fieldtype', data=fieldtype[1:])
        inDbase=pyasdf.ASDFDataSet(inasdffname)
        pers = self.attrs['period_array']
        minlon=self.attrs['minlon']
        maxlon=self.attrs['maxlon']
        minlat=self.attrs['minlat']
        maxlat=self.attrs['maxlat']
        dlon=self.attrs['dlon']
        dlat=self.attrs['dlat']
        fdict={ 'Tph': 2, 'Tgr': 3, 'Amp': 4}
        evLst=inDbase.events
        fieldLst=[]
        # prepare data
        for per in pers:
            print 'Computing gradient for: '+str(per)+' sec'
            del_per=per-int(per)
            if del_per==0.:
                persfx=str(int(per))+'sec'
            else:
                dper=str(del_per)
                persfx=str(int(per))+'sec'+dper.split('.')[1]
            working_per=workingdir+'/'+str(per)+'sec'
            per_group=group.require_group( name='%g_sec'%( per ) )
            evnumb=0
            for event in evLst:
                evnumb+=1
                evid='E%05d' % evnumb
                try:
                    subdset = inDbase.auxiliary_data[data_type][evid+'_'+channel][persfx]
                except KeyError:
                    print 'No travel time field for: '+evid
                    continue
                magnitude=event.magnitudes[0].mag; Mtype=event.magnitudes[0].magnitude_type
                event_descrip=event.event_descriptions[0].text+', '+event.event_descriptions[0].type
                evlo=event.origins[0].longitude; evla=event.origins[0].latitude
                if verbose: print 'Event: '+event_descrip+', '+Mtype+' = '+str(magnitude) 
                if evlo<0.: evlo+=360.
                dataArr = subdset.data.value
                fieldpair=[]
                field2d=field2d_earth.Field2d(minlon=minlon, maxlon=maxlon, dlon=dlon,
                        minlat=minlat, maxlat=maxlat, dlat=dlat, period=per, evlo=evlo, evla=evla, fieldtype=fieldtype, evid=evid)
                Zarr=dataArr[:, fdict[fieldtype]]
                distArr=dataArr[:, 6] # Note amplitude in added!!!
                field2d.read_array(lonArr=np.append(evlo, dataArr[:,0]), latArr=np.append(evla, dataArr[:,1]), ZarrIn=np.append(0., distArr/Zarr) )
                fieldpair.append(field2d)
                if amplplc:
                    field2dAmp=field2d_earth.Field2d(minlon=minlon, maxlon=maxlon, dlon=dlon,
                        minlat=minlat, maxlat=maxlat, dlat=dlat, period=per, evlo=evlo, evla=evla, fieldtype='Amp', evid=evid)
                    field2dAmp.read_array(lonArr=dataArr[:,0], latArr=dataArr[:,1], ZarrIn=dataArr[:, fdict['Amp']] )
                    fieldpair.append(field2dAmp)
                fieldLst.append(fieldpair)
        # Computing gradient with multiprocessing
        if len(fieldLst) > subsize:
            Nsub = int(len(fieldLst)/subsize)
            for isub in xrange(Nsub):
                print 'Subset:', isub,'in',Nsub,'sets'
                cfieldLst=fieldLst[isub*subsize:(isub+1)*subsize]
                HELMHOTZ = partial(helmhotz4mp, workingdir=workingdir, channel=channel, amplplc=amplplc)
                pool = multiprocessing.Pool(processes=nprocess)
                pool.map(HELMHOTZ, cfieldLst) #make our results with a map call
                pool.close() #we are not adding any more processes
                pool.join() #tell it to wait until all threads are done before going on
            cfieldLst=fieldLst[(isub+1)*subsize:]
            HELMHOTZ = partial(helmhotz4mp, workingdir=workingdir, channel=channel, amplplc=amplplc)
            pool = multiprocessing.Pool(processes=nprocess)
            pool.map(HELMHOTZ, cfieldLst) #make our results with a map call
            pool.close() #we are not adding any more processes
            pool.join() #tell it to wait until all threads are done before going on
        else:
            HELMHOTZ = partial(helmhotz4mp, workingdir=workingdir, channel=channel, amplplc=amplplc)
            pool = multiprocessing.Pool(processes=nprocess)
            pool.map(HELMHOTZ, fieldLst) #make our results with a map call
            pool.close() #we are not adding any more processes
            pool.join() #tell it to wait until all threads are done before going on
        # Read data into hdf5 dataset
        for per in pers:
            print 'Reading gradient data for: '+str(per)+' sec'
            working_per=workingdir+'/'+str(per)+'sec'
            per_group=group.require_group( name='%g_sec'%( per ) )
            evnumb=0
            for event in evLst:
                evnumb+=1
                evid='E%05d' % evnumb
                infname=working_per+'/'+evid+'_field2d.npz'
                if not os.path.isfile(infname): print 'No data for:', evid; continue
                InArr=np.load(infname)
                appV=InArr['arr_0']; reason_n=InArr['arr_1']; proAngle=InArr['arr_2']
                az=InArr['arr_3']; baz=InArr['arr_4']; Zarr=InArr['arr_5']
                if amplplc:
                    lplc_amp=InArr['arr_6']; corV=InArr['arr_7']
                evlo=event.origins[0].longitude; evla=event.origins[0].latitude
                # save data to hdf5 dataset
                event_group=per_group.require_group(name=evid)
                event_group.attrs.create(name = 'evlo', data=evlo)
                event_group.attrs.create(name = 'evla', data=evla)
                appVdset     = event_group.create_dataset(name='appV', data=appV)
                reason_ndset = event_group.create_dataset(name='reason_n', data=reason_n)
                proAngledset = event_group.create_dataset(name='proAngle', data=proAngle)
                azdset       = event_group.create_dataset(name='az', data=az)
                bazdset      = event_group.create_dataset(name='baz', data=baz)
                Tdset        = event_group.create_dataset(name='travelT', data=Zarr)
                if amplplc:
                    lplc_ampdset = event_group.create_dataset(name='lplc_amp', data=lplc_amp)
                    corV_dset = event_group.create_dataset(name='corV', data=corV)
        if deletetxt: shutil.rmtree(workingdir)
        return
    
    
    def eikonal_stack(self, runid=0, minazi=-180, maxazi=180, N_bin=20, threshmeasure=15, anisotropic=False, helmholtz=False, use_numba=True):
        """
        Stack gradient results to perform Eikonal Tomography
        =================================================================================================================
        ::: input parameters :::
        runid           - run id
        minazi/maxazi   - min/max azimuth for anisotropic parameters determination
        N_bin           - number of bins for anisotropic parameters determination
        anisotropic     - perform anisotropic parameters determination or not
        use_numba       - use numba for large array manipulation or not, faster and much less memory requirement
        -----------------------------------------------------------------------------------------------------------------
        version history:
            Dec 6th, 2016   - add function to use numba, faster and much less memory consumption
            Feb 7th, 2018   - bug fixed by adding signALL,
                                originally stdArr = np.sum( (weightALL-avgArr)**2, axis=0), 2018-02-07
        =================================================================================================================
        """
        pers            = self.attrs['period_array']
        minlon          = self.attrs['minlon']
        maxlon          = self.attrs['maxlon']
        minlat          = self.attrs['minlat']
        maxlat          = self.attrs['maxlat']
        dlon            = self.attrs['dlon']
        dlat            = self.attrs['dlat']
        Nlon            = int(self.attrs['Nlon'])
        Nlat            = int(self.attrs['Nlat'])
        nlat_grad       = self.attrs['nlat_grad']
        nlon_grad       = self.attrs['nlon_grad']
        nlat_lplc       = self.attrs['nlat_lplc']
        nlon_lplc       = self.attrs['nlon_lplc']
        group           = self['Eikonal_run_'+str(runid)]
        try:
            group_out   = self.create_group( name = 'Eikonal_stack_'+str(runid) )
        except ValueError:
            warnings.warn('Eikonal_stack_'+str(runid)+' exists! Will be recomputed!', UserWarning, stacklevel=1)
            del self['Eikonal_stack_'+str(runid)]
            group_out   = self.create_group( name = 'Eikonal_stack_'+str(runid) )
        group_out.attrs.create(name = 'anisotropic', data=anisotropic)
        group_out.attrs.create(name = 'N_bin', data=N_bin)
        group_out.attrs.create(name = 'minazi', data=minazi)
        group_out.attrs.create(name = 'maxazi', data=maxazi)
        group_out.attrs.create(name = 'fieldtype', data=group.attrs['fieldtype'])
        for per in pers:
            print 'Stacking Eikonal results for: '+str(per)+' sec'
            # if per != 24.:
            #     continue
            per_group   = group['%g_sec'%( per )]
            Nevent      = len(per_group.keys())
            Nmeasure    = np.zeros((Nlat-2*nlat_grad, Nlon-2*nlon_grad), dtype=np.int32)
            weightALL   = np.zeros((Nevent, Nlat-2*nlat_grad, Nlon-2*nlon_grad))
            slownessALL = np.zeros((Nevent, Nlat-2*nlat_grad, Nlon-2*nlon_grad))
            aziALL      = np.zeros((Nevent, Nlat-2*nlat_grad, Nlon-2*nlon_grad), dtype='float32')
            reason_nALL = np.zeros((Nevent, Nlat-2*nlat_grad, Nlon-2*nlon_grad))
            validALL    = np.zeros((Nevent, Nlat-2*nlat_grad, Nlon-2*nlon_grad), dtype='float32')
            for iev in range(Nevent):
                evid                = per_group.keys()[iev]
                event_group         = per_group[evid]
                reason_n            = event_group['reason_n'].value
                az                  = event_group['az'].value
                oneArr              = np.ones((Nlat-2*nlat_grad, Nlon-2*nlon_grad), dtype=np.int32)
                oneArr[reason_n!=0] = 0
                Nmeasure            += oneArr
                if helmholtz:
                    velocity            = event_group['corV'].value
                else:   
                    velocity            = event_group['appV'].value
                ##
                # # reason_n[(velocity > 4.0)]   = 3
                # # reason_n[(velocity < 2.0)]   = 3
                # # print np.where((velocity>5.)*(reason_n==0.))
                ##
                slowness                = np.zeros((Nlat-2*nlat_grad, Nlon-2*nlon_grad), dtype=np.float32)
                slowness[velocity!=0]   = 1./velocity[velocity!=0]
                slownessALL[iev, :, :]  = slowness
                reason_nALL[iev, :, :]  = reason_n
                aziALL[iev, :, :]       = az
                
                # ## debug
                # Ndebug = (velocity[(reason_n==0)*(velocity< 1.)]).size
                # if Ndebug != 0: 
                #     print evid, Ndebug 
            if Nmeasure.max()< threshmeasure:
                print ('No enough measurements for: '+str(per)+' sec')
                continue
            #-----------------------------------------------
            # Get weight for each grid point per event
            #-----------------------------------------------
            if use_numba:
                validALL[reason_nALL==0]    = 1
                weightALL                   = _get_azi_weight(aziALL, validALL)
                weightALL[reason_nALL!=0]   = 0
                weightALL[weightALL!=0]     = 1./weightALL[weightALL!=0]
                weightsum                   = np.sum(weightALL, axis=0)
            else:
                azi_event1                  = np.broadcast_to(aziALL, (Nevent, Nevent, Nlat-2*nlat_grad, Nlon-2*nlon_grad))
                azi_event2                  = np.swapaxes(azi_event1, 0, 1)
                validALL[reason_nALL==0]    = 1
                validALL4                   = np.broadcast_to(validALL, (Nevent, Nevent, Nlat-2*nlat_grad, Nlon-2*nlon_grad))
                # use numexpr for very large array manipulations
                del_aziALL                  = numexpr.evaluate('abs(azi_event1-azi_event2)')
                index_azi                   = numexpr.evaluate('(1*(del_aziALL<20)+1*(del_aziALL>340))*validALL4')
                weightALL                   = numexpr.evaluate('sum(index_azi, 0)')
                weightALL[reason_nALL!=0]   = 0
                weightALL[weightALL!=0]     = 1./weightALL[weightALL!=0]
                weightsum                   = np.sum(weightALL, axis=0)
            #-----------------------------------------------
            # reduce large weight to some value.
            #-----------------------------------------------
            avgArr                          = np.zeros((Nlat-2*nlat_grad, Nlon-2*nlon_grad))
            avgArr[Nmeasure!=0]             = weightsum[Nmeasure!=0]/Nmeasure[Nmeasure!=0]
            # bug fixed, Feb 7th, 2018
            signALL                         = weightALL.copy()
            signALL[signALL!=0]             = 1.
            stdArr                          = np.sum( signALL*(weightALL-avgArr)**2, axis=0)
            stdArr[Nmeasure!=0]             = stdArr[Nmeasure!=0]/Nmeasure[Nmeasure!=0]
            stdArr                          = np.sqrt(stdArr)
            threshhold                      = np.broadcast_to(avgArr+3.*stdArr, weightALL.shape)
            weightALL[weightALL>threshhold] = threshhold[weightALL>threshhold] # threshhold truncated weightALL
            #-----------------------------------------------
            # Compute mean/std of slowness
            #-----------------------------------------------
            weightsum                       = np.sum(weightALL, axis=0)
            weightsumALL                    = np.broadcast_to(weightsum, weightALL.shape)
            weightALL[weightsumALL!=0]      = weightALL[weightsumALL!=0]/weightsumALL[weightsumALL!=0] # weight over all events
            ###
            weightALL[weightALL==1.]        = 0.
            ###
            slownessALL2                    = slownessALL*weightALL
            slowness_sum                    = np.sum(slownessALL2, axis=0)
            slowness_sumALL                 = np.broadcast_to(slowness_sum, weightALL.shape)
            # new
            signALL                         = weightALL.copy()
            signALL[signALL!=0]             = 1.
            MArr                            = np.sum(signALL, axis=0)
            temp                            = weightALL*(slownessALL-slowness_sumALL)**2
            temp                            = np.sum(temp, axis=0)
            slowness_std                    = np.zeros(temp.shape)
            tind                            = (weightsum!=0)*(MArr!=1)
            slowness_std[tind]              = np.sqrt(temp[tind]/weightsum[tind]*MArr[tind]/(MArr[tind]-1))
            # old
            # w2sum                           = np.sum(weightALL**2, axis=0)
            # temp                            = weightALL*(slownessALL-slowness_sumALL)**2
            # temp                            = np.sum(temp, axis=0)
            # slowness_std                    = np.sqrt(temp/(1-w2sum))
            slowness_stdALL                 = np.broadcast_to(slowness_std, weightALL.shape)
            #-----------------------------------------------
            # discard outliers of slowness
            #-----------------------------------------------
            weightALLQC                     = weightALL.copy()
            index_outlier                   = (np.abs(slownessALL-slowness_sumALL))>2.*slowness_stdALL
            index_outlier                   += reason_nALL != 0
            weightALLQC[index_outlier]      = 0
            weightsumQC                     = np.sum(weightALLQC, axis=0)
            NmALL                           = np.sign(weightALLQC)
            NmeasureQC                      = np.sum(NmALL, axis=0)
            weightsumQCALL                  = np.broadcast_to(weightsumQC, weightALL.shape)
            weightALLQC[weightsumQCALL!=0]  = weightALLQC[weightsumQCALL!=0]/weightsumQCALL[weightsumQCALL!=0]
            temp                            = weightALLQC*slownessALL
            slowness_sumQC                  = np.sum(temp, axis=0)
            # new
            signALLQC                       = weightALLQC.copy()
            signALLQC[signALLQC!=0]         = 1.
            MArrQC                          = np.sum(signALLQC, axis=0)
            temp                            = weightALLQC*(slownessALL-slowness_sumQC)**2
            temp                            = np.sum(temp, axis=0)
            slowness_stdQC                  = np.zeros(temp.shape)
            tind                            = (weightsumQC!=0)*(MArrQC!=1)
            slowness_stdQC[tind]            = np.sqrt(temp[tind]/weightsumQC[tind]*MArrQC[tind]/(MArrQC[tind]-1))
            # old
            # # w2sumQC                         = np.sum(weightALLQC**2, axis=0)
            # # temp                            = weightALLQC*(slownessALL-slowness_sumQC)**2
            # # temp                            = np.sum(temp, axis=0)
            # # slowness_stdQC                  = np.sqrt(temp/(1-w2sumQC))
            #---------------------------------------------------------------
            # mask, velocity, and sem arrays of shape Nlat, Nlon
            #---------------------------------------------------------------
            mask                            = np.ones((Nlat, Nlon), dtype=np.bool)
            tempmask                        = (weightsumQC == 0)
            mask[nlat_grad:-nlat_grad, nlon_grad:-nlon_grad] \
                                            = tempmask
            vel_iso                         = np.zeros((Nlat, Nlon), dtype=np.float32)
            tempvel                         = slowness_sumQC.copy()
            tempvel[tempvel!=0]             = 1./ tempvel[tempvel!=0]
            vel_iso[nlat_grad:-nlat_grad, nlon_grad:-nlon_grad]\
                                            = tempvel
            # standard error of the mean
            slownessALL_temp                = slownessALL.copy()
            slownessALL_temp[slownessALL_temp==0.]\
                                            = 0.3
            temp                            = weightALLQC*(1./slownessALL-tempvel)**2
            temp                            = np.sum(temp, axis=0)
            tempsem                         = np.zeros(temp.shape)
            tind                            = (weightsumQC!=0)*(MArrQC!=1)
            tempsem[tind]                   = np.sqrt(temp[tind]/weightsumQC[tind]/(MArrQC[tind]-1))        
            # # # tempsem                         = slowness_stdQC/MArrQC
            # # # tempsem[slowness_sumQC!=0]      = tempsem[slowness_sumQC!=0]/(slowness_sumQC[slowness_sumQC!=0]**2)
            vel_sem                         = np.zeros((Nlat, Nlon), dtype=np.float32)
            vel_sem[nlat_grad:-nlat_grad, nlon_grad:-nlon_grad]\
                                            = tempsem
            # save isotropic velocity to database
            per_group_out                   = group_out.create_group( name='%g_sec'%( per ) )
            sdset                           = per_group_out.create_dataset(name='slowness', data=slowness_sumQC)
            s_stddset                       = per_group_out.create_dataset(name='slowness_std', data=slowness_stdQC)
            Nmdset                          = per_group_out.create_dataset(name='Nmeasure', data=Nmeasure)
            NmQCdset                        = per_group_out.create_dataset(name='NmeasureQC', data=NmeasureQC)
            maskdset                        = per_group_out.create_dataset(name='mask', data=mask)
            visodset                        = per_group_out.create_dataset(name='vel_iso', data=vel_iso)
            vstddset                        = per_group_out.create_dataset(name='vel_sem', data=vel_sem)
            #----------------------------------------------------------------------------
            # determine anisotropic parameters, need benchmark and further verification
            #----------------------------------------------------------------------------
            # debug, synthetic anisotropy
            # phi             = 44.
            # A               = 0.01
            # phi             = phi/180.*np.pi
            # tempazi         = aziALL/180.*np.pi
            # vALL            = np.broadcast_to(slowness_sumQC.copy(), slownessALL.shape)
            # # # index           = vALL!=0
            # vALL.setflags(write=1)
            # index           = vALL==0
            # # vALL[vALL!=0]   = 3.5
            # vALL[vALL!=0]   = 1./vALL[vALL!=0]
            # # return slownessALL, slowness_sumQC
            # vALL            = vALL + A*np.cos(2*(tempazi-phi))
            # vALL[index]     = 0.
            # slownessALL     = vALL.copy()
            # slownessALL[slownessALL!=0] = 1./slownessALL[slownessALL!=0]
            # slowness_sumQC[slowness_sumQC!=0]  = 1./3.5
            
            if anisotropic:
                NmeasureAni                 = np.zeros((Nlat-2*nlat_grad, Nlon-2*nlon_grad))
                total_near_neighbor         = Nmeasure[4:-4, 4:-4] + Nmeasure[:-8, :-8] + Nmeasure[8:, 8:] + Nmeasure[:-8, 4:-4] +\
                                Nmeasure[8:, 4:-4] + Nmeasure[4:-4, :-8] + Nmeasure[4:-4, 8:] + Nmeasure[8:, :-8] + Nmeasure[:-8, 8:]
                NmeasureAni[4:-4, 4:-4]     = total_near_neighbor # for quality control
                # initialization of anisotropic parameters
                d_bin                       = (maxazi-minazi)/N_bin
                histArr                     = np.zeros((N_bin, Nlat-2*nlat_grad, Nlon-2*nlon_grad))
                histArr_cutted              = histArr[:, 3:-3, 3:-3]
                slow_sum_ani                = np.zeros((N_bin, Nlat-2*nlat_grad, Nlon-2*nlon_grad))
                slow_sum_ani_cutted         = slow_sum_ani[:, 3:-3, 3:-3]
                slow_un                     = np.zeros((N_bin, Nlat-2*nlat_grad, Nlon-2*nlon_grad))
                slow_un_cutted              = slow_un[:, 3:-3, 3:-3]
                azi_11                      = aziALL[:, :-6, :-6]
                azi_12                      = aziALL[:, :-6, 3:-3]
                azi_13                      = aziALL[:, :-6, 6:]
                azi_21                      = aziALL[:, 3:-3, :-6]
                azi_22                      = aziALL[:, 3:-3, 3:-3]
                azi_23                      = aziALL[:, 3:-3, 6:]
                azi_31                      = aziALL[:, 6:, :-6]
                azi_32                      = aziALL[:, 6:, 3:-3]
                azi_33                      = aziALL[:, 6:, 6:]
                slowsumQC_cutted            = slowness_sumQC[3:-3, 3:-3]
                slownessALL_cutted          = slownessALL[:, 3:-3, 3:-3]
                index_outlier_cutted        = index_outlier[:, 3:-3, 3:-3]
                for ibin in xrange(N_bin):
                    sumNbin                     = (np.zeros((Nlat-2*nlat_grad, Nlon-2*nlon_grad)))[3:-3, 3:-3]
                    slowbin                     = (np.zeros((Nlat-2*nlat_grad, Nlon-2*nlon_grad)))[3:-3, 3:-3]
                    
                    ibin11                      = np.floor((azi_11-minazi)/d_bin)
                    temp1                       = 1*(ibin==ibin11)
                    temp1[index_outlier_cutted] = 0
                    temp2                       = temp1*(slownessALL_cutted-slowsumQC_cutted)
                    temp1                       = np.sum(temp1, 0)
                    temp2                       = np.sum(temp2, 0) #temp2[temp1!=0]=temp2[temp1!=0]/temp1[temp1!=0]
                    sumNbin                     +=temp1
                    slowbin                     +=temp2 #print temp2.max(), temp2.min() 

                    ibin12                      = np.floor((azi_12-minazi)/d_bin)
                    temp1                       = 1*(ibin==ibin12)
                    temp1[index_outlier_cutted] = 0
                    temp2                       = temp1*(slownessALL_cutted-slowsumQC_cutted)
                    temp1                       = np.sum(temp1, 0)
                    temp2                       = np.sum(temp2, 0)
                    sumNbin                     +=temp1
                    slowbin                     +=temp2
                    
                    ibin13                      = np.floor((azi_13-minazi)/d_bin)
                    temp1                       = 1*(ibin==ibin13)
                    temp1[index_outlier_cutted] = 0
                    temp2                       = temp1*(slownessALL_cutted-slowsumQC_cutted)
                    temp1                       = np.sum(temp1, 0)
                    temp2                       = np.sum(temp2, 0)
                    sumNbin                     +=temp1
                    slowbin                     +=temp2
                    
                    ibin21                      = np.floor((azi_21-minazi)/d_bin)
                    temp1                       = 1*(ibin==ibin21)
                    temp1[index_outlier_cutted] = 0
                    temp2                       = temp1*(slownessALL_cutted-slowsumQC_cutted)
                    temp1                       = np.sum(temp1, 0)
                    temp2                       = np.sum(temp2, 0)
                    sumNbin                     +=temp1
                    slowbin                     +=temp2
                    
                    ibin22                      = np.floor((azi_22-minazi)/d_bin)
                    temp1                       = 1*(ibin==ibin22)
                    temp1[index_outlier_cutted] = 0
                    temp2                       = temp1*(slownessALL_cutted-slowsumQC_cutted)
                    temp1                       = np.sum(temp1, 0)
                    temp2                       = np.sum(temp2, 0)
                    sumNbin                     +=temp1
                    slowbin                     +=temp2
                    
                    ibin23                      = np.floor((azi_23-minazi)/d_bin)
                    temp1                       = 1*(ibin==ibin23)
                    temp1[index_outlier_cutted] = 0
                    temp2                       = temp1*(slownessALL_cutted-slowsumQC_cutted)
                    temp1                       = np.sum(temp1, 0)
                    temp2                       = np.sum(temp2, 0)
                    sumNbin                     +=temp1
                    slowbin                     +=temp2
                    
                    ibin31                      = np.floor((azi_31-minazi)/d_bin)
                    temp1                       = 1*(ibin==ibin31)
                    temp1[index_outlier_cutted] = 0
                    temp2                       = temp1*(slownessALL_cutted-slowsumQC_cutted)
                    temp1                       = np.sum(temp1, 0)
                    temp2                       = np.sum(temp2, 0)
                    sumNbin                     +=temp1
                    slowbin                     +=temp2
                    
                    ibin32                      = np.floor((azi_32-minazi)/d_bin)
                    temp1                       = 1*(ibin==ibin32)
                    temp1[index_outlier_cutted] = 0
                    temp2                       = temp1*(slownessALL_cutted-slowsumQC_cutted)
                    temp1                       = np.sum(temp1, 0)
                    temp2                       = np.sum(temp2, 0)
                    sumNbin                     +=temp1
                    slowbin                     +=temp2
                    
                    ibin33                      = np.floor((azi_33-minazi)/d_bin)
                    temp1                       = 1*(ibin==ibin33)
                    temp1[index_outlier_cutted] = 0
                    temp2                       = temp1*(slownessALL_cutted-slowsumQC_cutted)
                    temp1                       = np.sum(temp1, 0)
                    temp2                       = np.sum(temp2, 0)
                    sumNbin                     +=temp1
                    slowbin                     +=temp2
                   
                    histArr_cutted[ibin, :, :]          = sumNbin
                    slow_sum_ani_cutted[ibin, :, :]     = slowbin
                
                slow_sum_ani_cutted[histArr_cutted>10]  = slow_sum_ani_cutted[histArr_cutted>10]/histArr_cutted[histArr_cutted>10]
                slow_sum_ani_cutted[histArr_cutted<=10] = 0
                # uncertainties
                slow_iso_std                            = np.broadcast_to(slowness_stdQC[3:-3, 3:-3], histArr_cutted.shape)
                slow_un_cutted[histArr_cutted>10]       = slow_iso_std[histArr_cutted>10]/np.sqrt(histArr_cutted[histArr_cutted>10])
                slow_un_cutted[histArr_cutted<=10]      = 0
                # convert std of slowness to std of speed
                temp                                    = np.broadcast_to(slowsumQC_cutted, slow_un_cutted.shape)
                temp                                    = ( temp + slow_sum_ani_cutted)**2
                slow_un_cutted[temp!=0]                 = slow_un_cutted[temp!=0]/temp[temp!=0]
                slow_sum_ani[:, 3:-3, 3:-3]             = slow_sum_ani_cutted
                slow_un[:, 3:-3, 3:-3]                  = slow_un_cutted
                slow_sum_ani[:, NmeasureAni<45]         = 0 # near neighbor quality control
                slow_un[:, NmeasureAni<45]              = 0
                histArr[:, 3:-3, 3:-3]                  = histArr_cutted
                # save data to database
                s_anidset       = per_group_out.create_dataset(name='slownessAni', data=slow_sum_ani)
                s_anistddset    = per_group_out.create_dataset(name='slownessAni_std', data=slow_un)
                histdset        = per_group_out.create_dataset(name='histArr', data=histArr)
                NmAnidset       = per_group_out.create_dataset(name='NmeasureAni', data=NmeasureAni)
        # debug, raw az and slowness
            if per == 24.:
                self.aziALL     = aziALL
                self.slownessALL=slownessALL
                self.index_outlier=index_outlier
        #
        return
    
    def debug_plot_azimuth(self, inlat, inlon):
        nlat_grad       = self.attrs['nlat_grad']
        nlon_grad       = self.attrs['nlon_grad']
        self._get_lon_lat_arr()
        index    = np.where((self.latArr==inlat)*(self.lonArr==inlon))
        print index
        index_outlier = self.index_outlier[:, index[0] - nlat_grad, index[1] - nlon_grad]
        slowness = self.slownessALL[:, index[0] - nlat_grad, index[1] - nlon_grad]
        azi      = self.aziALL[:, index[0] - nlat_grad, index[1] - nlon_grad]
        
        outaz    = azi[index_outlier==0]
        outslow  = slowness[index_outlier==0]
        return outaz, outslow
        
    def plot_azimuthal_single_point(self, inlat, inlon, runid, period, fitdata=True):
        
        dataid          = 'Eikonal_stack_'+str(runid)
        ingroup         = self[dataid]
        pers            = self.attrs['period_array']
        nlat_grad       = self.attrs['nlat_grad']
        nlon_grad       = self.attrs['nlon_grad']
        self._get_lon_lat_arr()
        index   = np.where((self.latArr==inlat)*(self.lonArr==inlon))
        if not period in pers:
            raise KeyError('period = '+str(period)+' not included in the database')
        pergrp          = ingroup['%g_sec'%( period )]
        slowAni         = pergrp['slownessAni'].value + pergrp['slowness'].value
        slowAnistd      = pergrp['slownessAni_std'].value
        outslowness     = slowAni[:, index[0] - nlat_grad, index[1] - nlon_grad]
        outslowness_std = slowAnistd[:, index[0] - nlat_grad, index[1] - nlon_grad]
        maxazi          = ingroup.attrs['maxazi']
        minazi          = ingroup.attrs['minazi']
        Nbin            = ingroup.attrs['N_bin']
        azArr           = np.mgrid[minazi:maxazi:Nbin*1j]
        if fitdata:
            indat           = (1./outslowness).reshape(1, Nbin)
            U               = np.zeros((Nbin, Nbin), dtype=np.float64)
            np.fill_diagonal(U, 1./outslowness_std)
            # np.fill_diagonal(U, 1.)
            # construct forward operator matrix
            tG              = np.ones((Nbin, 1), dtype=np.float64)
            tbaz            = np.pi*(azArr+180.)/180.

            tGsin2          = np.sin(tbaz*2)
            tGcos2          = np.cos(tbaz*2)
            G               = np.append(tG, tGsin2)
            G               = np.append(G, tGcos2)
            G               = G.reshape((3, Nbin))
            
            # tGsin4          = np.sin(tbaz*4.)
            # tGcos4          = np.cos(tbaz*4.)
            # G               = np.append(tG, tGsin2)
            # G               = np.append(G, tGcos2)
            # G               = np.append(G, tGsin4)
            # G               = np.append(G, tGcos4)
            # G               = G.reshape((5, Nbin))
            
            G               = G.T
            G               = np.dot(U, G)
            # data
            d               = indat.T
            d               = np.dot(U, d)
            # least square inversion
            model           = np.linalg.lstsq(G,d)[0]
            A0              = model[0]
            A1              = np.sqrt(model[1]**2 + model[2]**2)
            phi1            = np.arctan2(model[2], model[1])
            predat          = np.dot(G, model) * outslowness_std
            # predat          = predat*outslowness_std
        plt.errorbar(azArr+180., 1./outslowness, yerr=outslowness_std, fmt='o')
        if fitdata:
            plt.plot(azArr+180., predat, '-')
        plt.show()
        return indat, model
        
    def _numpy2ma(self, inarray, reason_n=None):
        """Convert input numpy array to masked array
        """
        if reason_n==None:
            outarray=ma.masked_array(inarray, mask=np.zeros(self.reason_n.shape) )
            outarray.mask[self.reason_n!=0]=1
        else:
            outarray=ma.masked_array(inarray, mask=np.zeros(reason_n.shape) )
            outarray.mask[reason_n!=0]=1
        return outarray     
    
    def _get_lon_lat_arr(self, ncut=0):
        """Get longitude/latitude array
        """
        minlon      = self.attrs['minlon']
        maxlon      = self.attrs['maxlon']
        minlat      = self.attrs['minlat']
        maxlat      = self.attrs['maxlat']
        dlon        = self.attrs['dlon']
        dlat        = self.attrs['dlat']
        self.lons   = np.arange((maxlon-minlon)/dlon+1-2*ncut)*dlon+minlon+ncut*dlon
        self.lats   = np.arange((maxlat-minlat)/dlat+1-2*ncut)*dlat+minlat+ncut*dlat
        self.Nlon   = self.lons.size
        self.Nlat   = self.lats.size
        self.lonArr, self.latArr = np.meshgrid(self.lons, self.lats)
        return
    
    def np2ma(self):
        """Convert numpy data array to masked data array
        """
        try:
            reason_n=self.reason_n
        except:
            raise AttrictError('No reason_n array!')
        self.vel_iso=self._numpy2ma(self.vel_iso)
        return
    
    
    def _get_basemap(self, projection='lambert', geopolygons=None, resolution='i'):
        """Get basemap for plotting results
        """
        # fig=plt.figure(num=None, figsize=(12, 12), dpi=80, facecolor='w', edgecolor='k')
        minlon      = self.attrs['minlon']
        maxlon      = self.attrs['maxlon']
        minlat      = self.attrs['minlat']
        maxlat      = self.attrs['maxlat']
        lat_centre  = (maxlat+minlat)/2.0
        lon_centre  = (maxlon+minlon)/2.0
        if projection=='merc':
            m       = Basemap(projection='merc', llcrnrlat=minlat-5., urcrnrlat=maxlat+5., llcrnrlon=minlon-5.,
                        urcrnrlon=maxlon+5., lat_ts=20, resolution=resolution)
            # m.drawparallels(np.arange(minlat,maxlat,dlat), labels=[1,0,0,1])
            # m.drawmeridians(np.arange(minlon,maxlon,dlon), labels=[1,0,0,1])
            m.drawparallels(np.arange(-80.0,80.0,5.0), labels=[1,0,0,1])
            m.drawmeridians(np.arange(-170.0,170.0,5.0), labels=[1,0,0,1])
            m.drawstates(color='g', linewidth=2.)
        elif projection=='global':
            m       = Basemap(projection='ortho',lon_0=lon_centre, lat_0=lat_centre, resolution=resolution)
            # m.drawparallels(np.arange(-80.0,80.0,10.0), labels=[1,0,0,1])
            # m.drawmeridians(np.arange(-170.0,170.0,10.0), labels=[1,0,0,1])
        elif projection=='regional_ortho':
            m1      = Basemap(projection='ortho', lon_0=minlon, lat_0=minlat, resolution='l')
            m       = Basemap(projection='ortho', lon_0=minlon, lat_0=minlat, resolution=resolution,\
                        llcrnrx=0., llcrnry=0., urcrnrx=m1.urcrnrx/mapfactor, urcrnry=m1.urcrnry/3.5)
            m.drawparallels(np.arange(-80.0,80.0,10.0), labels=[1,0,0,0],  linewidth=2,  fontsize=20)
            # m.drawparallels(np.arange(-90.0,90.0,30.0),labels=[1,0,0,0], dashes=[10, 5], linewidth=2,  fontsize=20)
            # m.drawmeridians(np.arange(10,180.0,30.0), dashes=[10, 5], linewidth=2)
            m.drawmeridians(np.arange(-170.0,170.0,10.0),  linewidth=2)
        elif projection=='lambert':
            distEW, az, baz = obspy.geodetics.gps2dist_azimuth(minlat, minlon, minlat, maxlon) # distance is in m
            distNS, az, baz = obspy.geodetics.gps2dist_azimuth(minlat, minlon, maxlat+2., minlon) # distance is in m
            m               = Basemap(width=distEW, height=distNS, rsphere=(6378137.00,6356752.3142), resolution='l', projection='lcc',\
                                lat_1=minlat, lat_2=maxlat, lon_0=lon_centre, lat_0=lat_centre+1)
            m.drawparallels(np.arange(-80.0,80.0,10.0), linewidth=1, dashes=[2,2], labels=[1,1,0,0], fontsize=15)
            m.drawmeridians(np.arange(-170.0,170.0,10.0), linewidth=1, dashes=[2,2], labels=[0,0,1,0], fontsize=15)
            # m.drawparallels(np.arange(-80.0,80.0,10.0), linewidth=0.5, dashes=[2,2], labels=[1,0,0,0], fontsize=5)
            # m.drawmeridians(np.arange(-170.0,170.0,10.0), linewidth=0.5, dashes=[2,2], labels=[0,0,0,1], fontsize=5)
        m.drawcoastlines(linewidth=1.0)
        m.drawcountries(linewidth=1.)
        # m.drawmapboundary(fill_color=[1.0,1.0,1.0])
        m.fillcontinents(lake_color='#99ffff',zorder=0.2)
        m.drawstates()
        m.drawmapboundary(fill_color="white")
        try:
            geopolygons.PlotPolygon(inbasemap=m)
        except:
            pass
        return m
    
    def plot(self, runid, datatype, period, clabel='', cmap='cv', projection='lambert', geopolygons=None, vmin=None, vmax=None, showfig=True):
        """plot maps from the tomographic inversion
        =================================================================================================================
        ::: input parameters :::
        runtype         - type of run (0 - smooth run, 1 - quality controlled run)
        runid           - id of run
        datatype        - datatype for plotting
        period          - period of data
        clabel          - label of colorbar
        cmap            - colormap
        projection      - projection type
        geopolygons     - geological polygons for plotting
        vmin, vmax      - min/max value of plotting
        showfig         - show figure or not
        =================================================================================================================
        """
        # vdict       = {'ph': 'C', 'gr': 'U'}
        datatype        = datatype.lower()
        dataid          = 'Eikonal_stack_'+str(runid)
        ingroup         = self[dataid]
        pers            = self.attrs['period_array']
        self._get_lon_lat_arr()
        if not period in pers:
            raise KeyError('period = '+str(period)+' not included in the database')
        pergrp          = ingroup['%g_sec'%( period )]
        if datatype == 'vel' or datatype=='velocity' or datatype == 'v':
            datatype    = 'vel_iso'
        elif datatype == 'sem' or datatype == 'un' or datatype == 'uncertainty':
            datatype    = 'vel_sem'
        elif datatype=='std':
            datatype    = 'slowness_std'
        try:
            data    = pergrp[datatype].value
            if datatype=='slowness_std':
                data2   = data.copy()
                data    = np.zeros(self.lonArr.shape)
                data[1:-1, 1:-1] = data2
        except:
            outstr      = ''
            for key in pergrp.keys():
                outstr  +=key
                outstr  +=', '
            outstr      = outstr[:-1]
            raise KeyError('Unexpected datatype: '+datatype+\
                           ', available datatypes are: '+outstr)
        mask        = pergrp['mask'].value
        mdata       = ma.masked_array(data, mask=mask )
        #-----------
        # plot data
        #-----------
        m           = self._get_basemap(projection=projection, geopolygons=geopolygons)
        x, y        = m(self.lonArr, self.latArr)
        try:
            shapefname  = '/scratch/summit/life9360/ALASKA_work/fault_maps/qfaults'
            m.readshapefile(shapefname, 'faultline', linewidth=2)
        except:
            pass
        if cmap == 'ses3d':
            cmap        = colormaps.make_colormap({0.0:[0.1,0.0,0.0], 0.2:[0.8,0.0,0.0], 0.3:[1.0,0.7,0.0],0.48:[0.92,0.92,0.92],
                            0.5:[0.92,0.92,0.92], 0.52:[0.92,0.92,0.92], 0.7:[0.0,0.6,0.7], 0.8:[0.0,0.0,0.8], 1.0:[0.0,0.0,0.1]})
        elif cmap == 'cv':
            import pycpt
            cmap    = pycpt.load.gmtColormap('./cv.cpt')
        elif os.path.isfile(cmap):
            import pycpt
            cmap    = pycpt.load.gmtColormap(cmap)
        im          = m.pcolormesh(x, y, mdata, cmap=cmap, shading='gouraud', vmin=vmin, vmax=vmax)
        cb          = m.colorbar(im, "bottom", size="3%", pad='2%')
        cb.set_label(clabel, fontsize=12, rotation=0)
        plt.suptitle(str(period)+' sec', fontsize=20)
        cb.ax.tick_params(labelsize=15)
        if showfig:
            plt.show()
        return
    
    def plot_vel_iso(self, projection='lambert', fastaxis=False, geopolygons=None, showfig=True, vmin=2.9, vmax=3.5):
        """Plot isotropic velocity
        """
        m=self._get_basemap(projection=projection, geopolygons=geopolygons)
        x, y=m(self.lonArr, self.latArr)
        cmap = colormaps.make_colormap({0.0:[0.1,0.0,0.0], 0.2:[0.8,0.0,0.0], 0.3:[1.0,0.7,0.0],0.48:[0.92,0.92,0.92],
            0.5:[0.92,0.92,0.92], 0.52:[0.92,0.92,0.92], 0.7:[0.0,0.6,0.7], 0.8:[0.0,0.0,0.8], 1.0:[0.0,0.0,0.1]})
        im=m.pcolormesh(x, y, self.vel_iso, cmap=cmap, shading='gouraud', vmin=vmin, vmax=vmax)
        cb = m.colorbar(im, "bottom", size="3%", pad='2%')
        cb.set_label('V'+self.fieldtype+'(km/s)', fontsize=12, rotation=0)
        plt.title(str(self.period)+' sec', fontsize=20)
        # if fastaxis:
        #     try:
        #         self.plot_fast_axis(inbasemap=m)
        #     except:
        #         pass
        if showfig:
            plt.show()
            
    def get_data4plot(self, period, runid=0, ncut=2, Nmin=15):
        """
        Get data for plotting
        =======================================================================================
        ::: input parameters :::
        period              - period
        runid               - run id
        ncut                - number of cutted edge points
        Nmin                - minimum required number of measurements
        ---------------------------------------------------------------------------------------
        generated data arrays:
        ----------------------------------- isotropic version ---------------------------------
        self.vel_iso        - isotropic velocity
        self.slowness_std   - slowness standard deviation
        self.Nmeasure       - number of measurements at each grid point
        self.reason_n       - array to represent valid/invalid data points
        ---------------------------------- anisotropic version --------------------------------
        include all the array above(but will be converted to masked array), and
        self.N_bin          - number of bins
        self.minazi/maxazi  - min/max azimuth
        self.slownessAni    - anisotropic slowness perturbation categorized for each bin
        self.slownessAni_std- anisotropic slowness perturbation std
        self.histArr        - number of measurements for each bins
        self.NmeasureAni    - number of measurements for near neighbor points
        =======================================================================================
        """
        self._get_lon_lat_arr(ncut=ncut)
        Nlon=self.attrs['Nlon']
        Nlat=self.attrs['Nlat']
        subgroup=self['Eikonal_stack_'+str(runid)+'/%g_sec'%( period )]
        self.period=period
        slowness=subgroup['slowness'].value
        self.vel_iso=np.zeros((Nlat-4, Nlon-4))
        self.vel_iso[slowness!=0]=1./slowness[slowness!=0]
        self.Nmeasure=subgroup['Nmeasure'].value
        self.slowness_std=subgroup['slowness_std'].value
        self.reason_n=np.zeros((Nlat-4, Nlon-4))
        self.reason_n[self.Nmeasure<Nmin]=1
        group=self['Eikonal_stack_'+str(runid)]
        self.anisotropic=group.attrs['anisotropic']
        self.fieldtype=group.attrs['fieldtype']
        if self.anisotropic:
            self.N_bin=group.attrs['N_bin']
            self.minazi=group.attrs['minazi']
            self.maxazi=group.attrs['maxazi']
            self.slownessAni=subgroup['slownessAni'].value
            self.slownessAni_std=subgroup['slownessAni_std'].value
            self.histArr=subgroup['histArr'].value
            self.NmeasureAni=subgroup['NmeasureAni'].value
        return
        

def eikonal4mp(infield, workingdir, channel):
    working_per     = workingdir+'/'+str(infield.period)+'sec'
    outfname        = infield.evid+'_'+infield.fieldtype+'_'+channel+'.lst'
    infield.interp_surface(workingdir=working_per, outfname=outfname)
    infield.check_curvature(workingdir=working_per, outpfx=infield.evid+'_'+channel+'_')
    infield.gradient_qc(workingdir=working_per, inpfx=infield.evid+'_'+channel+'_', nearneighbor=True, cdist=None)
    outfname_npz    = working_per+'/'+infield.evid+'_field2d'
    infield.write_binary(outfname=outfname_npz)
    return

def helmhotz4mp(infieldpair, workingdir, channel, amplplc):
    tfield=infieldpair[0]
    working_per=workingdir+'/'+str(tfield.period)+'sec'
    outfname=tfield.evid+'_'+tfield.fieldtype+'_'+channel+'.lst'
    tfield.interp_surface(workingdir=working_per, outfname=outfname)
    tfield.check_curvature(workingdir=working_per, outpfx=tfield.evid+'_'+channel+'_')
    tfield.gradient_qc(workingdir=working_per, inpfx=tfield.evid+'_'+channel+'_', nearneighbor=True, cdist=None)
    outfname_npz=working_per+'/'+tfield.evid+'_field2d'
    if not amplplc: tfield.write_binary(outfname=outfname_npz)
    if amplplc:
        field2dAmp=infieldpair[1]
        outfnameAmp=field2dAmp.evid+'_Amp_'+channel+'.lst'
        field2dAmp.interp_surface(workingdir=working_per, outfname=outfnameAmp)
        field2dAmp.gradient()
        field2dAmp.cut_edge(1,1)
        field2dAmp.Laplacian()
        field2dAmp.cut_edge(1,1)
        field2dAmp.get_lplc_amp()
        slownessApp=-np.ones(tfield.appV.shape)
        slownessApp[tfield.appV!=0]=1./tfield.appV[tfield.appV!=0]
        temp=slownessApp**2-field2dAmp.lplc_amp
        temp[temp<0]=0
        slownessCor=np.sqrt(temp)
        corV=np.zeros(slownessCor.shape)
        corV[slownessCor!=0]=1./slownessCor[slownessCor!=0]
        tfield.corV=corV
        tfield.lplc_amp=field2dAmp.lplc_amp
        tfield.write_binary(outfname=outfname_npz, amplplc=amplplc)
    return 

